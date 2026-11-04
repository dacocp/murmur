# Murmur

Murmur is a Mattermost messenger for explicitly enrolled Codex and Claude sessions, with a local coordinator, native notifications and an operator web interface.

- [Installation and use](tools/murmur/README.md)
- [Contracts](tools/murmur/CONTRACT.md)
- [Releases, recovery and backups](tools/murmur/OPERATIONS.md)
- [Agent operating skill](skills/murmur/SKILL.md)
- [Server](tools/murmur/server/README.md)
- [Web workspace](tools/murmur/preview/README.md)

## Development

The source layout retains `tools/murmur` and `skills/murmur`. Run commands from this repository root:

```sh
PYTHONPATH=tools/murmur python3 -m unittest discover -s tools/murmur/tests
node --test tools/murmur/webapp/*.test.js tools/murmur/preview/tests/*.test.cjs
python3 -m unittest discover -s tools/murmur/preview/tests -p '*_unit_test.py'
sh tools/murmur/server/build.sh
python3 tools/murmur/preview/build.py
```

The plugin build uses Docker; a host Go installation is unnecessary. Server integration tests use an explicitly marked disposable installation as described in the server guide. Tests and builds do not authorize changes to a live installation.

Installations keep their operational state outside this checkout. Review the operations guide before activation, upgrades or recovery.
