import os
import yaml
import logging


class Registry:
    def __init__(self, storage_filepath):
        self._storage_filepath = storage_filepath
        self._data = dict()
        self._is_dirty = True
        self._validate_file_exists()

    def write(self, key, content):
        if key in self._data and self._data[key] != content:
            self._is_dirty = True
        self._data[key] = content

    def read(self, key):
        return self._data.get(key, None)

    def flush(self):
        if self._is_dirty:
            with open(self._storage_filepath, "w") as storage_file:
                yaml.dump(self._data, storage_file)
        self._is_dirty = False

    def _refresh(self):
        with open(self._storage_filepath) as registry:
            self._data = yaml.load(registry)
            if not isinstance(self._data, dict):
                raise Exception("Invalid registry file: %s" % (self._storage_filepath,))
        self._is_dirty = False

    def _validate_file_exists(self):
        if os.path.exists(self._storage_filepath):
            self._refresh()
        else:
            dirname = os.path.dirname(self._storage_filepath)
            if not os.path.exists(dirname):
                logging.info("Creating the registry directory in %(dirname)s", dict(dirname=dirname))
                os.mkdir(dirname)
            if not os.path.exists(self._storage_filepath):
                logging.info("Creating the registry file...", dict(dirname=dirname))
                self.flush()
