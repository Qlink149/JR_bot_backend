import json
import logging
import os
from datetime import datetime
from threading import Lock

from bson import ObjectId

from qlink_chatbot.constants import SKIP_FIELDS_LOGGER


class SingletonLogger:
    _instance = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super().__new__(cls, *args, **kwargs)
                cls._instance._initialize_logger()
            return cls._instance

    def _initialize_logger(self):
        self.logger = logging.getLogger("SingletonLogger")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        if self.logger.handlers:
            return

        formatter = JsonFormatter()

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(formatter)
        self.logger.addHandler(stream_handler)

        # Vercel production filesystem is read-only.
        # File logging only works safely in /tmp.
        log_dir = os.getenv("LOG_DIR", "/tmp/logs")
        log_file_path = os.path.join(log_dir, "app.log")

        try:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(filename=log_file_path, mode="a")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except OSError:
            # Never crash app because of logging
            self.logger.warning("File logging disabled because filesystem is read-only.")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord):
        log_data = {
            "logged_at": datetime.now().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "function_name": record.funcName,
            "file_path": record.pathname,
            "line_number": record.lineno,
        }

        extra_fields = {
            key: value
            for key, value in vars(record).items()
            if key not in SKIP_FIELDS_LOGGER
        }

        log_data.update(extra_fields)

        def custom_serializer(obj):
            if isinstance(obj, ObjectId):
                return str(obj)
            elif isinstance(obj, datetime):
                return obj.isoformat()
            elif hasattr(obj, "model"):
                return {
                    "model": getattr(obj, "model", None),
                    "usage": getattr(obj, "usage", None),
                }
            elif hasattr(obj, "__dict__"):
                return str(obj)

            return f"<Unserializable object of type {obj.__class__.__name__}>"

        return json.dumps(log_data, indent=2, default=custom_serializer) + "\n**************\n"

# Fixed for Vercel production read-only filesystem

logger = SingletonLogger().logger