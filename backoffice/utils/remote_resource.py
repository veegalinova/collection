from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from ruyaml import YAML
from typing_extensions import assert_never

from .s3_client import Client
from .s3_structure import Details, Log, LogCategory, Status, StatusName

yaml = YAML(typ="safe")


@dataclass
class RemoteResource:
    """A representation of a bioimage.io resource
    (**not** a specific staged or published version of it)"""

    client: Client
    """Client to connect to remote storage"""
    id: str
    """resource identifier"""

    def _get_latest_stage_nr(self) -> int | None:
        staged = list(map(int, self.client.ls(f"{self.id}/staged/", only_folders=True)))
        if not staged:
            return None
        else:
            return max(staged)

    def get_latest_staged_version(self) -> StagedVersion | None:
        """Get a representation of the latest staged version
        (the one with the highest stage nr)"""
        v = self._get_latest_stage_nr()
        if v is None:
            return None
        else:
            return StagedVersion(client=self.client, id=self.id, version=v)

    def stage_new_version(self, package_url: str) -> StagedVersion:
        """Stage the content at `package_url` as a new resource version candidate."""
        v = self._get_latest_stage_nr()
        if v is None:
            v = 1

        ret = StagedVersion(client=self.client, id=self.id, version=v)
        ret.set_status("staging", f"unzipping {package_url} to {ret.folder}")

        # Download the model zip file
        try:
            remotezip = urllib.request.urlopen(package_url)
        except Exception:
            logger.error("failed to open {}", package_url)
            raise

        zipinmemory = io.BytesIO(remotezip.read())

        # Unzip the zip file
        zipobj = zipfile.ZipFile(zipinmemory)

        rdf = yaml.load(zipobj.open("rdf.yaml").read().decode())
        if (rdf_id := rdf.get("id")) is None:
            rdf["id"] = ret.id
        elif rdf_id != ret.id:
            raise ValueError(
                f"Expected package for {ret.id}, "
                f"but got packaged {rdf_id} ({package_url})"
            )

        # overwrite version information
        rdf["version"] = ret.version

        if rdf.get("id_emoji") is None:
            # TODO: set `id_emoji` according to id
            raise ValueError(f"RDF in {package_url} is missing `id_emoji`")

        for filename in zipobj.namelist():
            file_data = zipobj.open(filename).read()
            path = f"{ret.folder}files/{filename}"
            self.client.put(path, io.BytesIO(file_data), length=len(file_data))

        return ret


@dataclass
class RemoteResourceVersion(RemoteResource, ABC):
    """Base class for a resource version (`StagedVersion` or `PublishedVersion`)"""

    version: int
    """version number"""

    @property
    @abstractmethod
    def version_prefix(self) -> str:
        """a prefix to distinguish independent staged and published `version` numbers"""
        pass

    @property
    def folder(self) -> str:
        """The S3 (sub)prefix of this version
        (**sub**)prefix, because the client may prefix this prefix"""
        return f"{self.id}/{self.version_prefix}{self.version}/"

    @property
    def rdf_url(self) -> str:
        """rdf.yaml download URL"""
        return self.client.get_file_url(f"{self.folder}files/rdf.yaml")

    def get_log(self) -> Log:
        path = f"{self.folder}log.json"
        log_data = self.client.load_file(path)
        if log_data is None:
            log: Log = {}
        else:
            log = json.loads(log_data)
            assert isinstance(log, dict)

        return log

    def _get_details(self) -> Details:
        details_data = self.client.load_file(f"{self.folder}details.json")
        if details_data is None:
            details: Details = {
                "messages": [],
                "status": self._create_status("unknown", "no status information found"),
            }
        else:
            details = json.load(io.BytesIO(details_data))

        return details

    def _set_details(self, details: Details):
        self.client.put_json(f"{self.folder}details.json", details)

    def get_messages(self):
        details = self._get_details()
        return details["messages"]

    def add_message(self, author: str, text: str):
        logger.info("msg from {}: text", author)
        details = self._get_details()
        now = datetime.now().isoformat()
        details["messages"].append({"author": author, "text": text, "time": now})
        self._set_details(details)

    def set_status(self, name: StatusName, description: str) -> None:
        details = self._get_details()
        details["status"] = self._create_status(name, description)
        self._set_details(details)

    @staticmethod
    def _create_status(name: StatusName, description: str) -> Status:
        num_steps = 5
        if name == "unknown":
            step = 1
            num_steps = 1
        elif name == "staging":
            step = 1
        elif name == "testing":
            step = 2
        elif name == "awaiting review":
            step = 3
        elif name == "publishing":
            step = 4
        elif name == "published":
            step = 5
        else:
            assert_never(name)

        return Status(
            name=name, description=description, step=step, num_steps=num_steps
        )

    def get_status(self) -> Status:
        """get the current status"""
        details = self._get_details()
        return details["status"]

    def add_log_entry(
        self,
        category: LogCategory,
        content: list[Any] | dict[Any, Any] | int | float | str | None | bool,
    ):
        """add log entry"""
        log = self.get_log()
        entries = log.setdefault(category, [])
        now = datetime.now().isoformat()
        entries.append({"timestamp": now, "log": content})
        self._set_log(log)

    def _set_log(self, log: Log) -> None:
        self.client.put_json(f"{self.folder}log.json", log)


@dataclass
class StagedVersion(RemoteResourceVersion):
    """A staged resource version"""

    version: int
    """stage number (**not** future resource version)"""

    @property
    def version_prefix(self):
        """The 'staged/' prefix identifies the `version` as a stage number
        (opposed to a published resource version)."""
        return "staged/"

    def await_review(self):
        """set status to 'awaiting review'"""
        self.set_status(
            "awaiting review",
            description=(
                "Thank you for your contribution! "
                "Our bioimage.io maintainers will take a look soon."
            ),
        )

    def publish(self) -> PublishedVersion:
        """publish this staged version candidate as the next resource version"""
        # get next version and update versions.json
        versions_path = f"{self.id}/versions.json"
        versions_data = self.client.load_file(versions_path)
        if versions_data is None:
            versions: dict[str, Any] = {}
            next_version = 1
        else:
            versions = json.loads(versions_data)
            next_version = max(map(int, versions)) + 1

        logger.debug("Publishing {} as version {}", self.folder, next_version)

        assert next_version not in versions, (next_version, versions)

        versions[str(next_version)] = {}
        updated_versions_data = json.dumps(versions).encode()
        self.client.put(
            versions_path,
            io.BytesIO(updated_versions_data),
            length=len(updated_versions_data),
        )
        ret = PublishedVersion(client=self.client, id=self.id, version=next_version)

        # move rdf.yaml and set version in it
        staged_rdf_path = f"{self.folder}files/rdf.yaml"
        rdf_data = self.client.load_file(staged_rdf_path)
        rdf = yaml.load(rdf_data)
        rdf["version"] = ret.version
        stream = io.StringIO()
        yaml.dump(rdf, stream)
        rdf_data = stream.read().encode()
        self.client.put(
            f"{ret.folder}files/rdf.yaml", io.BytesIO(rdf_data), length=len(rdf_data)
        )
        self.client.rm_obj(staged_rdf_path)

        # move all other files
        self.client.mv_dir(self.folder, ret.folder)

        # remove all preceding staged versions
        self.client.rm_dir(f"{self.id}/{self.version_prefix}")
        return ret


@dataclass
class PublishedVersion(RemoteResourceVersion):
    """A representation of a published resource version"""

    @property
    def version_prefix(self):
        """published versions do not have a prefix"""
        return ""
