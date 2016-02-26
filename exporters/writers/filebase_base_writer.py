import datetime
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from exporters.writers.base_writer import BaseWriter

MD5_FILE_NAME = 'md5checksum.md5'


class FilebaseBaseWriter(BaseWriter):
    """
    This writer is a base writer providing common methods to all file based writers

    - filebase
        Path to store the exported files

    """
    supported_options = {
        'filebase': {'type': basestring},
        'generate_md5': {'type': bool, 'default': True}
    }

    def __init__(self, options, *args, **kwargs):
        super(FilebaseBaseWriter, self).__init__(options, *args, **kwargs)
        self.filebase = self.read_option('filebase')
        self.writer_metadata['written_files'] = []
        self.md5_file_name = None
        if self.read_option('generate_md5'):
            self.tmp_md5_folder = tempfile.mkdtemp()
            self.md5_file_name = os.path.join(self.tmp_md5_folder, MD5_FILE_NAME)

    def write(self, path, key, file_name=False):
        """
        Receive path to temp dump file and group key, and write it to the proper location.
        """
        raise NotImplementedError

    def get_file_suffix(self, path, prefix):
        """
        This method is a fallback to provide valid random filenames
        """
        return str(uuid.uuid4())

    def create_filebase_name(self, group_info, extension='gz', file_name=None):
        """
        Returns filebase and file valid name
        """
        normalized = [re.sub('\W', '_', s) for s in group_info]
        filebase = self.read_option('filebase')
        filebase = filebase.format(date=datetime.datetime.now(),
                                                           groups=normalized)
        filebase = datetime.datetime.now().strftime(filebase)
        filebase_path, prefix = os.path.split(filebase)
        if not file_name:
            file_name = prefix + self.get_file_suffix(filebase_path, prefix) + '.' + extension
        return filebase_path, file_name

    def _append_md5_info(self, write_info):
        file_name = self.writer_metadata['written_files'][-1]
        with open(file_name, 'r') as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        with open(self.md5_file_name, 'a') as f:
            f.write('{} {}'.format(md5, file_name)+'\n')

    def _write(self, key):
        write_info = self.write_buffer.pack_buffer(key)
        self.write(write_info.get('compressed_path'), self.write_buffer.grouping_info[key]['membership'])
        if self.md5_file_name:
            self._append_md5_info(write_info)
        self.write_buffer.clean_tmp_files(key, write_info.get('compressed_path'))

    def close(self):
        if self.md5_file_name:
            try:
                self.write(self.md5_file_name, None, file_name=MD5_FILE_NAME)
            finally:
                shutil.rmtree(self.tmp_md5_folder)
        super(FilebaseBaseWriter, self).close()
