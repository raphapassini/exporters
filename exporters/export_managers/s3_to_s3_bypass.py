import datetime
import logging
import os
import shutil
import tempfile
import uuid
from contextlib import closing

from boto.exception import S3ResponseError

from exporters.default_retries import retry_long
from exporters.export_managers.base_bypass import RequisitesNotMet, BaseBypass
from exporters.module_loader import ModuleLoader
from exporters.progress_callback import BotoUploadProgress
from exporters.readers.s3_reader import get_bucket, S3BucketKeysFetcher


class S3BypassState(object):

    def __init__(self, config):
        self.config = config
        module_loader = ModuleLoader()
        self.state = module_loader.load_persistence(config.persistence_options)
        self.state_position = self.state.get_last_position()
        if not self.state_position:
            self.pending = S3BucketKeysFetcher(self.config.reader_options['options']).pending_keys()
            self.done = []
            self.skipped = []
            self.state.commit_position(self._get_state())
        else:
            self.pending = self.state_position['pending']
            self.done = []
            self.skipped = self.state_position['done']
            self.keys = self.pending

    def _get_state(self):
        return {'pending': self.pending, 'done': self.done, 'skipped': self.skipped}

    def commit_copied_key(self, key):
        self.pending.remove(key)
        self.done.append(key)
        self.state.commit_position(self._get_state())

    def pending_keys(self):
        return self.pending

    def delete(self):
        self.state.delete()


class S3Bypass(BaseBypass):
    """
    Bypass executed by default when data source and data destination are S3 buckets. It should be
    transparent to user. Conditions are:

        - S3Reader and S3Writer are used on configuration.
        - No filter modules are set up.
        - No transform module is set up.
        - No grouper module is set up.
        - S3 Writer has not a items_limit set in configuration.
        - S3 Writer has default items_per_buffer_write and size_per_buffer_write per default.

    This bypass tries to directly copy the S3 keys between the read and write buckets. If
    is is not possible due to permission issues, it will download the key from the read bucket
    and directly upload it to the write bucket.
    """

    def __init__(self, config):
        super(S3Bypass, self).__init__(config)
        self.copy_mode = True
        self.tmp_folder = None
        self.bypass_state = None
        self.logger = logging.getLogger('bypass_logger')
        self.logger.setLevel(logging.INFO)

    def _raise_conditions_not_met(self, reason):
        self.logger.warning('Skipping S3 file copy optimization bypass because of %s' % reason)
        raise RequisitesNotMet

    def meets_conditions(self):
        if not self.config.reader_options['name'].endswith('S3Reader') or not self.config.writer_options['name'].endswith('S3Writer'):
            raise RequisitesNotMet
        if not self.config.filter_before_options['name'].endswith('NoFilter'):
            self._raise_conditions_not_met('custom filter configured')
        if not self.config.filter_after_options['name'].endswith('NoFilter'):
            self._raise_conditions_not_met('custom filter configured')
        if not self.config.transform_options['name'].endswith('NoTransform'):
            self._raise_conditions_not_met('custom transform configured')
        if not self.config.grouper_options['name'].endswith('NoGrouper'):
            self._raise_conditions_not_met('custom grouper configured')
        if self.config.writer_options['options'].get('items_limit'):
            self._raise_conditions_not_met('items limit configuration (items_limit)')
        if self.config.writer_options['options'].get('items_per_buffer_write'):
            self._raise_conditions_not_met('buffer limit configuration (items_per_buffer_write)')
        if self.config.writer_options['options'].get('size_per_buffer_write'):
            self._raise_conditions_not_met('buffer limit configuration (size_per_buffer_write)')

    def _get_filebase(self, writer_options):
        dest_filebase = writer_options['filebase'].format(datetime.datetime.now())
        dest_filebase = datetime.datetime.now().strftime(dest_filebase)
        return dest_filebase

    def bypass(self):
        from copy import deepcopy
        reader_options = self.config.reader_options['options']
        writer_options = self.config.writer_options['options']
        dest_bucket = get_bucket(**writer_options)
        dest_filebase = self._get_filebase(writer_options)
        self.bypass_state = S3BypassState(self.config)
        source_bucket = get_bucket(**reader_options)
        pending_keys = deepcopy(self.bypass_state.pending_keys())
        user_id = dest_bucket.connection.get_canonical_user_id()
        try:
            for key in pending_keys:
                dest_key_name = '{}/{}'.format(dest_filebase, key.split('/')[-1])
                self._copy_key(dest_bucket, dest_key_name, source_bucket, key, user_id)
                self.bypass_state.commit_copied_key(key)
                logging.log(logging.INFO,
                            'Copied key {} to dest: s3://{}/{}'.format(key, dest_bucket.name, dest_key_name))
            if writer_options.get('save_pointer'):
                self._update_last_pointer(dest_bucket, writer_options.get('save_pointer'), writer_options.get('filebase'))

        finally:
            if self.tmp_folder:
                shutil.rmtree(self.tmp_folder)

    @retry_long
    def _write_s3_pointer(self, dest_bucket, save_pointer, filebase):
        with closing(dest_bucket.new_key(save_pointer)) as key:
            key.set_contents_from_string(filebase)

    def _update_last_pointer(self, dest_bucket, save_pointer, filebase):
        filebase = filebase.format(date=datetime.datetime.now())
        filebase = datetime.datetime.now().strftime(filebase)
        self._write_s3_pointer(dest_bucket, save_pointer, filebase)

    def _key_has_permissions(self, user_id, key):
        policy = key.get_acl()
        for grant in policy.acl.grants:
            if grant.id == user_id:
                return True
        return False

    def _add_permissions(self, user_id, key):
        key.add_user_grant('READ', user_id)

    def _clean_permissions(self, user_id, key):
        policy = key.get_acl()
        policy.acl.grants = [x for x in policy.acl.grants if not x.id == user_id]
        key.set_acl(policy)

    def _ensure_copy_key(self, dest_bucket, dest_key_name, source_bucket, key_name, user_id):
        key = source_bucket.get_key(key_name)
        permissions_handling = not self._key_has_permissions(user_id, key)
        if permissions_handling:
            self._add_permissions(user_id, key)
        dest_bucket.copy_key(dest_key_name, source_bucket.name, key_name)
        if permissions_handling:
            self._clean_permissions(user_id, key)

    @retry_long
    def _copy_key(self, dest_bucket, dest_key_name, source_bucket, key_name, user_id):
        akey = source_bucket.get_key(key_name)
        if akey.get_metadata('total'):
            self.increment_items(int(akey.get_metadata('total')))
        else:
            self.valid_total_count = False
        self._ensure_copy_key(dest_bucket, dest_key_name, source_bucket, key_name, user_id)

    def close(self):
        self.bypass_state.delete()
