"""Configure a native store and import, transfer or inspect its records."""
from native_runtime import record_import, record_transfer, store_configuration
from native_runtime.store_configuration import REVIEWED_CONFIGURE


class StoreOperations:
    def __init__(self, system):
        self.system = system

    configuration_descriptor = staticmethod(store_configuration.configuration_descriptor)

    def import_records(self, spec):
        return record_import.import_records(self.system, spec)

    def configure(self, spec):
        return store_configuration.configure(self.system, spec)

    def export(self, ident, output):
        return record_transfer.export(self.system, ident, output)

    def receive(self, path, ident, account, expected):
        return record_transfer.receive(self.system, path, ident, account, expected)

    def inspect(self, ident):
        return record_transfer.inspect(self.system, ident)
