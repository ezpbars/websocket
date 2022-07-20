import unittest

try:
    import helper
except:
    import tests.helper
import main


class Test(unittest.TestCase):
    def test_tests(self):
        self.assertTrue(True)

    def test_hello(self):
        self.assertEqual(main.root(), {"message": "Hello World"})


if __name__ == "__main__":
    unittest.main()
