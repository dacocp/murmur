"""Versioned Mattermost plugin client. Mutations are never blindly retried."""

import json
import hashlib
import http.client
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .protocol import ATTENTION_VERSION, MANAGED_CONVERSATION_READ_VERSION


class BackendError(RuntimeError):
    def __init__(self, message, *, uncertain=False, status=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class Backend:
    def __init__(self, url, token, timeout=15):
        self.url = url.rstrip("/")
        if not self.url.endswith("/plugins/murmur/v1"):
            self.url += "/plugins/murmur/v1"
        self.token = token
        self.timeout = timeout

    def request(self, method, path, payload=None, **query):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        })
        request_timeout = self.timeout
        if method == "GET" and path == "/health":
            request_timeout = min(request_timeout, 2)
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read(65536)).get("error", str(error))
            except (ValueError, AttributeError):
                message = str(error)
            raise BackendError(message, status=error.code,
                               uncertain=method != "GET" and
                               (error.code >= 500 or (error.code == 409 and "uncertain" in message))) from error
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise BackendError(str(error), uncertain=method != "GET") from error

    def register(self, actor, name, parent=None, *, mode, username_prefix=None,
                 enrollment_id=None, lineage=None):
        payload = dict(actor=actor, name=name, parent=parent, mode=mode)
        if username_prefix is not None:
            payload["username_prefix"] = username_prefix
        if enrollment_id is not None:
            payload["enrollment_id"] = enrollment_id
        if lineage is not None:
            payload["lineage"] = lineage
        return self.request("POST", "/register", payload)

    def agent_page(self, query="", limit=20, after=None):
        return self.request("GET", "/agents", query=query, limit=limit, after=after)

    def agents(self, query="", limit=20, after=None):
        return self.agent_page(query, limit, after)["agents"]

    def conversations(self, actor):
        return self.request("GET", "/conversations", actor=actor)["conversations"]

    def conversation_directory(self, actor, query="", kind=None, agent=None, limit=20, after=None):
        return self.request("GET", "/conversations/directory", actor=actor, query=query,
                            kind=kind, agent=agent, limit=limit, after=after,
                            read_version=MANAGED_CONVERSATION_READ_VERSION)

    def channels(self, actor, query="", limit=20, after=None):
        return self.request("GET", "/channels", actor=actor, query=query, limit=limit, after=after)

    def changes(self, actor, cursor=0, generation=None, limit=100):
        return self.request("GET", "/changes", actor=actor, cursor=cursor,
                            generation=generation, limit=limit, attention_version=ATTENTION_VERSION)

    def posts(self, actor, channel, limit=100, before=None):
        return self.request("GET", f"/conversations/{channel}/posts", actor=actor,
                            limit=limit, before=before, read_version=MANAGED_CONVERSATION_READ_VERSION)

    def post(self, actor, post_id):
        return self.request("GET", "/posts/" + post_id, actor=actor,
                            read_version=MANAGED_CONVERSATION_READ_VERSION)["post"]

    def root(self, actor, post_id):
        return self.request("GET", "/posts/" + post_id, actor=actor, as_root="true",
                            read_version=MANAGED_CONVERSATION_READ_VERSION)["post"]

    def saved(self, actor):
        return self.request("GET", "/saved", actor=actor,
                            read_version=MANAGED_CONVERSATION_READ_VERSION)["saved"]

    def set_saved(self, actor, post_id, starred):
        return self.request("POST", "/saved/" + post_id, dict(actor=actor, starred=starred),
                            read_version=MANAGED_CONVERSATION_READ_VERSION)

    def thread(self, actor, root):
        return self.thread_page(actor, root, limit=200)["posts"]

    def thread_page(self, actor, root, limit=20, before=None):
        return self.request("GET", "/threads/" + root, actor=actor, limit=limit, before=before,
                            read_version=MANAGED_CONVERSATION_READ_VERSION)

    def threads(self, actor, channel, limit=20, before=None):
        return self.request("GET", f"/conversations/{channel}/threads", actor=actor, limit=limit, before=before,
                            read_version=MANAGED_CONVERSATION_READ_VERSION)

    def operation(self, actor, op_id):
        return self.request("GET", "/operations/" + op_id, actor=actor)

    def mutate(self, action, actor, op_id, **payload):
        if action != "post":
            payload.pop("causal", None)
        paths = {"post": "/posts", "conversation": "/conversations", "operator_dm": "/operator-dm"}
        if action in ("join", "leave"):
            path = f'/conversations/{payload.pop("channel_id")}/{action}'
        else:
            path = paths[action]
        return self.request("POST", path, dict(actor=actor, op_id=op_id, **payload))

    def _open_attachment(self, method, path, data=None, **query):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        # Binary endpoints never forward authorization to a redirect target.
        try:
            return urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read(65536)).get("error", str(error))
            except (ValueError, AttributeError):
                message = str(error)
            uncertain = method != "GET" and (300 <= error.code < 400 or error.code >= 500
                or error.code == 409)
            raise BackendError(message, uncertain=uncertain, status=error.code) from error
        except (OSError, urllib.error.URLError) as error:
            raise BackendError(str(error), uncertain=method != "GET") from error

    def _attachment_json(self, method, path, data=None, **query):
        try:
            with self._open_attachment(method, path, data, **query) as response:
                return json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise BackendError(str(error), uncertain=method != "GET") from error

    def attachment_limits(self):
        value = self._attachment_json("GET", "/health")
        capabilities = value.get("capabilities") if isinstance(value, dict) else None
        limits = capabilities.get("attachments") if isinstance(capabilities, dict) else None
        if (not isinstance(limits, dict) or limits.get("enabled") is not True or type(limits.get("max_file_size")) is not int
                or limits["max_file_size"] <= 0 or type(limits.get("max_files_per_post")) is not int
                or limits["max_files_per_post"] <= 0):
            raise BackendError("This server does not support attachment uploads or has disabled them")
        return limits

    def upload(self, actor, op_id, channel_id, source, data):
        if len(data) != source["size"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("Upload bytes do not match the frozen attachment")
        return self._attachment_json("POST", "/files", data, actor=actor, op_id=op_id,
            channel_id=channel_id, filename=source["filename"], size=source["size"], sha256=source["sha256"])

    @staticmethod
    def _file_path(post_id, file_id):
        for value in (post_id, file_id):
            if not isinstance(value, str) or not value or value in (".", ".."):
                raise ValueError("A post ID and attached file ID are required")
        return "/posts/" + urllib.parse.quote(post_id, safe="") + "/files/" + urllib.parse.quote(file_id, safe="")

    def file_info(self, actor, post_id, file_id):
        value = self._attachment_json("GET", self._file_path(post_id, file_id) + "/info", actor=actor,
                                      read_version=MANAGED_CONVERSATION_READ_VERSION)
        info = value.get("file") if isinstance(value, dict) else None
        if (not isinstance(info, dict) or info.get("id") != file_id
                or type(info.get("size")) is not int or info["size"] < 0):
            raise BackendError("Server returned invalid metadata for the requested attachment")
        return info

    def download(self, actor, post_id, file_id, output):
        info = self.file_info(actor, post_id, file_id)
        target = Path(output).expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise ValueError("Download output already exists; choose another path")
        temporary = None
        try:
            with self._open_attachment("GET", self._file_path(post_id, file_id), actor=actor,
                                       read_version=MANAGED_CONVERSATION_READ_VERSION) as response:
                length = response.headers.get("Content-Length")
                checksum = response.headers.get("X-Content-SHA256", "").lower()
                if (length is not None and length != str(info["size"])):
                    raise BackendError("Attachment response size differs from its metadata")
                if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
                    raise BackendError("Attachment response lacks a valid content checksum")
                fd, temporary = tempfile.mkstemp(prefix=".murmur-download-", dir=target.parent)
                digest, size = hashlib.sha256(), 0
                with os.fdopen(fd, "wb") as stream:
                    while chunk := response.read(min(65536, info["size"] - size + 1)):
                        size += len(chunk)
                        if size > info["size"]:
                            raise BackendError("Attachment response exceeds its metadata size")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if size != info["size"] or digest.hexdigest() != checksum:
                    raise BackendError("Attachment download failed size or checksum verification")
                # Atomic creation, not replacement: concurrent output creation is preserved.
                os.link(temporary, target)
            return {"post_id": post_id, "file_id": file_id, "path": str(target),
                    "size": size, "sha256": digest.hexdigest()}
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise BackendError(str(error)) from error
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
