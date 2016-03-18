import datetime
import logging
import shutil
from contextlib import closing, contextmanager
from exporters.default_retries import retry_long
from exporters.export_managers.base_bypass import RequisitesNotMet, BaseBypass
from exporters.module_loader import ModuleLoader
from exporters.progress_callback import BotoUploadProgress
from exporters.readers.s3_reader import get_bucket, S3BucketKeysFetcher
from exporters.utils import TmpFile


def _add_permissions(user_id, key):
    key.add_user_grant('READ', user_id)


def _clean_permissions(user_id, key):
    policy = key.get_acl()
    policy.acl.grants = [x for x in policy.acl.grants if not x.id == user_id]
    key.set_acl(policy)


def _key_has_permissions(user_id, key):
    policy = key.get_acl()
    for grant in policy.acl.grants:
        if grant.id == user_id:
            return True
    return False


@contextmanager
def key_permissions(user_id, key):
    permissions_handling = not _key_has_permissions(user_id, key)
    if permissions_handling:
        _add_permissions(user_id, key)
    try:
        yield
    finally:
        if permissions_handling:
            _clean_permissions(user_id, key)


class S3BypassState(object):

    def __init__(self, config, aws_key, aws_secret):
        module_loader = ModuleLoader()
        self.state = module_loader.load_persistence(config.persistence_options)
        self.state_position = self.state.get_last_position()
        if not self.state_position:
            self.pending = S3BucketKeysFetcher(config.reader_options['options'], aws_key, aws_secret).pending_keys()
            self.done = []
            self.skipped = []
            self.stats = {'total_count': 0}
            self.state.commit_position(self._get_state())
        else:
            self.pending = self.state_position['pending']
            self.done = []
            self.skipped = self.state_position['done']
            self.keys = self.pending
            self.stats = self.state_position.get('stats', {'total_count': 0})

    def _get_state(self):
        return dict(pending=self.pending, done=self.done, skipped=self.skipped,
                    stats=self.stats)

    def commit_copied_key(self, key):
        self.pending.remove(key)
        self.done.append(key)
        self.state.commit_position(self._get_state())

    def increment_items(self, items_number):
        self.stats['total_count'] += items_number

    def pending_keys(self):
        return self.pending

    def delete(self):
        self.state.delete()


class InvalidKeyIntegrityCheck(Exception):
    """
    Exception thrown when two s3 keys have different md5 checksums
    """


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
        reader_aws_key = self.read_option('reader', 'aws_access_key_id', 'EXPORTERS_S3READER_AWS_KEY')
        reader_aws_secret = self.read_option('reader', 'aws_secret_access_key', 'EXPORTERS_S3READER_AWS_SECRET')

        writer_aws_key = self.read_option('writer', 'aws_access_key_id', 'EXPORTERS_S3WRITER_AWS_LOGIN')
        writer_aws_secret = self.read_option('writer', 'aws_secret_access_key', 'EXPORTERS_S3WRITER_AWS_SECRET')

        writer_options = self.config.writer_options['options']
        dest_bucket = get_bucket(self.read_option('writer', 'bucket'), writer_aws_key, writer_aws_secret)
        dest_filebase = self._get_filebase(writer_options)
        self.bypass_state = S3BypassState(self.config, reader_aws_key, reader_aws_secret)
        self.total_items = self.bypass_state.stats['total_count']

        source_bucket = get_bucket(self.read_option('reader', 'bucket'), reader_aws_key, reader_aws_secret)
        pending_keys = deepcopy(self.bypass_state.pending_keys())
        try:
            for key in pending_keys:
                dest_key_name = '{}/{}'.format(dest_filebase, key.split('/')[-1])
                self._copy_key(dest_bucket, dest_key_name, source_bucket, key)
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

    def _ensure_copy_key(self, dest_bucket, dest_key_name, source_bucket, key_name):
        from boto.exception import S3ResponseError
        key = source_bucket.get_key(key_name)
        try:
            user_id = dest_bucket.connection.get_canonical_user_id()
            with key_permissions(user_id, key):
                dest_bucket.copy_key(dest_key_name, source_bucket.name, key_name)
        except S3ResponseError:
            self.logger.warning('No direct copy supported for key {}.'.format(key_name))
            self._copy_without_permissions(dest_bucket, dest_key_name, source_bucket, key_name)
        # Using a second try catch, as they are independent operations
        try:
            dest_key = dest_bucket.get_key(dest_key_name)
            self._ensure_proper_key_permissions(dest_key)
            self._check_copy_integrity(key, dest_bucket, dest_key)
        except S3ResponseError:
            self.logger.warning('We have no READ_ACP/WRITE_ACP permissions')

    def _check_copy_integrity(self, source_key, dest_bucket, dest_key):
        if source_key.etag != dest_key.etag:
            raise InvalidKeyIntegrityCheck('Key {} and key {} md5 checksums are different. {} != {}'
                                           .format(source_key.name, dest_key.name, source_key.etag, dest_key.etag))

    def _ensure_proper_key_permissions(self, key):
        key.set_acl('bucket-owner-full-control')

    def _get_md5(self, key, tmp_filename):
        from boto.utils import compute_md5
        import re
        md5 = None
        md5_from_metadata = key.get_metadata('md5')
        if md5_from_metadata:
            match = re.match("\(\'(.*)\', u\'(.*)\', (.*)\)", str(md5_from_metadata))
            if match:
                groups = match.groups()
                md5 = (groups[0], unicode(groups[1]), int(groups[2]))
        # If it's not in metadata, let's compute it
        if md5 is None:
            with open(tmp_filename) as f:
                md5 = compute_md5(f)
        return md5

    def _copy_without_permissions(self, dest_bucket, dest_key_name, source_bucket, key_name):
        key = source_bucket.get_key(key_name)
        with TmpFile() as tmp_filename:
            key.get_contents_to_filename(tmp_filename)
            dest_key = dest_bucket.new_key(dest_key_name)
            progress = BotoUploadProgress(self.logger)
            md5 = self._get_md5(key, tmp_filename)
            dest_key.set_contents_from_filename(tmp_filename, cb=progress, md5=md5)

    @retry_long
    def _copy_key(self, dest_bucket, dest_key_name, source_bucket, key_name):
        akey = source_bucket.get_key(key_name)
        if akey.get_metadata('total'):
            self.increment_items(int(akey.get_metadata('total')))
            self.bypass_state.increment_items(int(akey.get_metadata('total')))
        else:
            self.valid_total_count = False
        self._ensure_copy_key(dest_bucket, dest_key_name, source_bucket, key_name)

    def close(self):
        self.bypass_state.delete()
