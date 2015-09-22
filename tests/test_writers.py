import random
import unittest
from exporters.records.base_record import BaseRecord
from exporters.writers.base_writer import BaseWriter
from exporters.writers.console_writer import ConsoleWriter


class BaseWriterTest(unittest.TestCase):

    def setUp(self):
        self.options = {
            'log_level': 'DEBUG',
            'logger_name': 'export-pipeline'
        }
        self.writer = BaseWriter(self.options)

    def tearDown(self):
        self.writer.close_writer()

    def test_write_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.writer.write('', '')


class ConsoleWriterTest(unittest.TestCase):

    def setUp(self):
        self.options = {
            'log_level': 'DEBUG',
            'logger_name': 'export-pipeline'
        }
        self.writer = ConsoleWriter(self.options)

    def tearDown(self):
        self.writer.close_writer()

    def test_write_console(self):
        items_to_write = []
        for i in range(0, 10):
            item = BaseRecord()
            item['key'] = i
            item['value'] = random.randint(0, 10000)
            items_to_write.append(item)

        self.writer.write_batch(items_to_write)
