"""The new subcommands parse and route to the right ops."""
import unittest

from link.cli import build_parser


class CliSurfaceTest(unittest.TestCase):
    def test_role_and_remove_parse(self):
        from link import cli
        parser = build_parser()
        args = parser.parse_args(["role", "dev_" + "a" * 16, "admin"])
        self.assertEqual(args.func, cli.cmd_role)
        self.assertEqual(args.role, "admin")
        args = parser.parse_args(["remove", "Sofia"])
        self.assertEqual(args.func, cli.cmd_remove)

    def test_new_subcommands_parse(self):
        parser = build_parser()
        for argv in (["name"], ["name", "Sofia"], ["knocks"],
                     ["grant", "Sofia"], ["deny", "dev_" + "a" * 16],
                     ["invite", "--door"], ["join", "--name", "Sofia",
                                            "--invite",
                                            "team-x#DOOR-" + "A" * 26]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func), argv)

    def test_deny_is_grant_with_the_flag_set(self):
        parser = build_parser()
        deny = parser.parse_args(["deny", "Sofia"])
        grant = parser.parse_args(["grant", "Sofia"])
        self.assertTrue(deny.deny)
        self.assertFalse(grant.deny)


if __name__ == "__main__":
    unittest.main()
