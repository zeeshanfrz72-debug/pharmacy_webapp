from contextlib import contextmanager
from contextvars import ContextVar


_current_batch = ContextVar("ledger_transaction_batch", default=None)


def current_batch():
    return _current_batch.get()


@contextmanager
def use_batch(batch):
    token = _current_batch.set(batch)
    try:
        yield
    finally:
        _current_batch.reset(token)
