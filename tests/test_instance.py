import sys
from pathlib import Path
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from instance import SingleInstance


class InstanceTests(unittest.TestCase):
    def test_second_listener_is_rejected_and_close_releases_reservation(self):
        name = 'Local\\ABI_Scout_Test_' + uuid.uuid4().hex
        first = SingleInstance(name)
        second = None
        try:
            self.assertFalse(first.already_running)
            second = SingleInstance(name)
            self.assertTrue(second.already_running)
            second.close()
            third = SingleInstance(name)
            try:
                self.assertTrue(third.already_running)
            finally:
                third.close()
        finally:
            if second:
                second.close()
            first.close()
        replacement = SingleInstance(name)
        try:
            self.assertFalse(replacement.already_running)
        finally:
            replacement.close()
