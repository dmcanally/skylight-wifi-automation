from __future__ import annotations

import logging
import traceback
from collections.abc import Iterable


class SecretRedactionFilter(logging.Filter):
    """Replace any currently known secret value before a record is emitted."""

    def __init__(self) -> None:
        super().__init__()
        self._values: tuple[str, ...] = ()

    def update(self, values: Iterable[str]) -> None:
        current = set(self._values)
        for value in values:
            if isinstance(value, str) and len(value) >= 4:
                current.add(value)
        self._values = tuple(sorted(current, key=len, reverse=True))

    def install(self) -> None:
        root = logging.getLogger()
        if self not in root.filters:
            root.addFilter(self)
        # Logger filters on the root do not run for records created on a child
        # logger. A record factory sees every record before any handler.
        current = logging.getLogRecordFactory()
        if getattr(current, "_redacts_secrets", False):
            return
        redactor = self

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = current(*args, **kwargs)
            redactor.filter(record)
            return record

        factory._redacts_secrets = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)

    def scrub_text(self, text: str) -> str:
        redacted = text
        for secret in self._values:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted

    def _scrub(self, value: object) -> object:
        if isinstance(value, str):
            return self.scrub_text(value)
        rendered = str(value)
        scrubbed = self.scrub_text(rendered)
        if scrubbed != rendered:
            return scrubbed
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        record.msg = self.scrub_text(str(record.msg))
        if isinstance(record.args, dict):
            record.args = {key: self._scrub(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(self._scrub(arg) for arg in record.args)
        if record.exc_info:
            text = "".join(traceback.format_exception(*record.exc_info))
            scrubbed = self.scrub_text(text)
            if scrubbed != text:
                record.exc_text = scrubbed
                record.exc_info = None
                record.stack_info = None
        return True


REDACTOR = SecretRedactionFilter()
