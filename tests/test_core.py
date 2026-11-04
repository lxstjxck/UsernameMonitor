import tempfile
import unittest
from pathlib import Path

from username_monitor.names import load_names
from username_monitor.store import Store


class TestNames(unittest.TestCase):
    def test_parse_dedupe_validate(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'usernames.txt'
            path.write_text('# comment\n@Alpha5\nalpha5\nhello_world\naaaa\nname-with-dash\n\n')
            names, invalid = load_names(path)
            self.assertEqual(names, ['alpha5', 'hello_world'])
            self.assertEqual(invalid, ['aaaa', 'name-with-dash'])


class TestStore(unittest.TestCase):
    def test_transition_and_error_preserves_status(self):
        with tempfile.TemporaryDirectory() as root:
            with Store(Path(root) / 'db.sqlite3') as store:
                prev, changed = store.record('alpha5', 'occupied')
                self.assertIsNone(prev)
                self.assertTrue(changed)
                prev, changed = store.record('alpha5', 'occupied')
                self.assertEqual(prev, 'occupied')
                self.assertFalse(changed)
                store.mark_error('alpha5', 'TimeoutError')
                prev, changed = store.record('alpha5', 'free')
                self.assertEqual(prev, 'occupied')
                self.assertTrue(changed)
                prev, changed = store.record('alpha5', 'free')
                self.assertFalse(changed)
                self.assertEqual(store.summary()[0][1], 'free')

    def test_round_robin_without_forgetting(self):
        with tempfile.TemporaryDirectory() as root:
            with Store(Path(root) / 'db.sqlite3') as store:
                selected = store.select(['alpha5', 'bravo5', 'charl5'], 2)
                self.assertEqual(selected, ['alpha5', 'bravo5'])
                store.record('alpha5', 'occupied')
                store.record('bravo5', 'occupied')
                self.assertEqual(store.select(['alpha5', 'bravo5', 'charl5'], 2)[0], 'charl5')


if __name__ == '__main__':
    unittest.main()
