"""In-memory test storage with assertions for privacy/deletion journal ordering."""
import copy
from storage import initial_state, validate_state
from config import PublisherError


class FakeStore:
    def __init__(self):
        self.durable = initial_state()
        self.blobs = {}
        self.public_manifest = None
        self.events = []
        self.fail_upload = False
        self.fail_publish = False
        self.fail_delete = False
        self.fail_save_at = None
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.durable)

    def save(self, state):
        validate_state(state)
        self.saves += 1
        if self.saves == self.fail_save_at:
            raise PublisherError("simulated_durability_error")
        self.durable = copy.deepcopy(state)
        self.events.append(("save", copy.deepcopy(state)))

    def exists(self, path):
        self.events.append(("exists", path))
        return path in self.blobs

    def upload(self, path, content, mime):
        assert path in self.durable["managed"], "Upload must have durable ownership intent first"
        if self.fail_upload:
            raise PublisherError("simulated_upload_error")
        self.blobs[path] = content
        self.events.append(("upload", path))

    def publish(self, data):
        assert self.durable["transaction"] is not None, "Publication must be journaled first"
        if self.fail_publish:
            raise PublisherError("simulated_publish_error")
        self.public_manifest = copy.deepcopy(data)
        self.events.append(("publish", data))

    def delete(self, path):
        assert path in self.durable["pending_cleanup"]
        assert path not in self.public_manifest["images"], "Never delete a publicly referenced copy"
        if self.fail_delete:
            raise PublisherError("simulated_delete_error")
        self.blobs.pop(path, None)
        self.events.append(("delete", path))
