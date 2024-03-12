from __future__ import annotations

import io
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, Optional, Type, TypeVar

from loguru import logger
from ruyaml import YAML
from typing_extensions import assert_never

from backoffice.s3_structure.chat import Chat
from backoffice.s3_structure.log import Logs

from .s3_client import Client
from .s3_structure.versions import (
    AcceptedStatus,
    AwaitingReviewStatus,
    ChangesRequestedStatus,
    PublishedStagedStatus,
    PublishedStatus,
    PublishedVersionDetails,
    PublishNumber,
    StagedVersionDetails,
    StagedVersionStatus,
    StageNumber,
    SupersededStatus,
    TestingStatus,
    UnpackedStatus,
    UnpackingStatus,
    Versions,
)

yaml = YAML(typ="safe")

VersionSpecificJsonFileT = TypeVar("VersionSpecificJsonFileT", Logs, Chat)
JsonFileT = TypeVar("JsonFileT", Versions, Logs, Chat)

NumberT = TypeVar("NumberT", StageNumber, PublishNumber)


@dataclass
class RemoteResource:
    """A representation of a bioimage.io resource
    (**not** a specific staged or published version of it)"""

    client: Client
    """Client to connect to remote storage"""
    id: str
    """resource identifier"""

    @property
    def resource_folder(self) -> str:
        """The S3 (sub)prefix of this resource"""
        return f"{self.id}/"

    @property
    def folder(self) -> str:
        """The S3 (sub)prefix of this resource (or resource version)"""
        return self.resource_folder

    def get_versions(self) -> Versions:
        return self._get_version_agnostic_json(Versions)

    def get_latest_stage_number(self) -> Optional[StageNumber]:
        versions = self.get_versions()
        if not versions.staged:
            return None
        else:
            return max(versions.staged)

    def get_latest_staged_version(self) -> Optional[StagedVersion]:
        """Get a representation of the latest staged version
        (the one with the highest stage number)"""
        nr = self.get_latest_stage_number()
        if nr is None:
            return None
        else:
            return StagedVersion(client=self.client, id=self.id, number=nr)

    def stage_new_version(self, package_url: str) -> StagedVersion:
        """Stage the content at `package_url` as a new resource version candidate."""
        nr = self.get_latest_stage_number()
        if nr is None:
            nr = StageNumber(1)

        ret = StagedVersion(client=self.client, id=self.id, number=nr)
        ret.unpack(package_url=package_url)
        return ret

    def _get_version_agnostic_json(self, typ: Type[Versions]) -> Versions:
        return self._get_json(typ, f"{self.resource_folder}{typ.__name__.lower()}.json")

    def _get_version_specific_json(
        self, typ: Type[VersionSpecificJsonFileT]
    ) -> VersionSpecificJsonFileT:
        return self._get_json(typ, f"{self.folder}{typ.__name__.lower()}.json")

    def _get_json(self, typ: Type[JsonFileT], path: str) -> JsonFileT:
        data = self.client.load_file(path)
        if data is None:
            return typ()
        else:
            return typ.model_validate_json(data)

    def _extend_version_agnostic_json(
        self,
        extension: Versions,
    ):
        self._extend_json(
            extension,
            f"{self.resource_folder}{extension.__class__.__name__.lower()}.json",
        )

    def _extend_version_specific_json(
        self,
        extension: VersionSpecificJsonFileT,
    ):
        self._extend_json(
            extension, f"{self.folder}{extension.__class__.__name__.lower()}.json"
        )

    def _extend_json(self, extension: JsonFileT, path: str):
        logger.info("Extending {} with {}", path, extension)
        current = self._get_json(extension.__class__, path)
        _ = current.extend(extension)
        self.client.put_pydantic(path, current)


@dataclass
class RemoteResourceVersion(RemoteResource, Generic[NumberT], ABC):
    """Base class for a resource version (`StagedVersion` or `PublishedVersion`)"""

    number: NumberT
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
        return f"{self.id}/{self.version_prefix}{self.number}/"

    @property
    def rdf_url(self) -> str:
        """rdf.yaml download URL"""
        return self.client.get_file_url(f"{self.folder}files/rdf.yaml")

    def get_log(self) -> Logs:
        return self._get_version_specific_json(Logs)

    def get_chat(self) -> Chat:
        return self._get_version_specific_json(Chat)

    def extend_log(
        self,
        extension: Logs,
    ):
        """extend log file"""
        self._extend_version_specific_json(extension)


@dataclass
class StagedVersion(RemoteResourceVersion[StageNumber]):
    """A staged resource version"""

    number: StageNumber
    """stage number (**not** future resource version)"""

    @property
    def version_prefix(self):
        """The 'staged/' prefix identifies the `version` as a stage number
        (opposed to a published resource version)."""
        return "staged/"

    def unpack(self, package_url: str):
        # ensure we have a chat.json
        self._extend_version_specific_json(self._get_version_specific_json(Chat))

        # ensure we have a logs.json
        self._extend_version_specific_json(self._get_version_specific_json(Logs))

        # set first status (this also write versions.json)
        self._set_status(
            UnpackingStatus(description=f"unzipping {package_url} to {self.folder}")
        )

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
            rdf["id"] = self.id
        elif rdf_id != self.id:
            raise ValueError(
                f"Expected package for {self.id}, "
                f"but got packaged {rdf_id} ({package_url})"
            )

        # overwrite version information
        rdf["version_number"] = self.number

        if rdf.get("id_emoji") is None:
            # TODO: set `id_emoji` according to id
            raise ValueError(f"RDF in {package_url} is missing `id_emoji`")

        for filename in zipobj.namelist():
            file_data = zipobj.open(filename).read()
            path = f"{self.folder}files/{filename}"
            self.client.put(path, io.BytesIO(file_data), length=len(file_data))

        self._set_status(UnpackedStatus())

    def set_testing_status(self, description: str):
        self._set_status(TestingStatus(description=description))

    def await_review(self):
        """set status to 'awaiting review'"""
        self._set_status(AwaitingReviewStatus())

    def request_changes(self, reason: str):
        self._set_status(ChangesRequestedStatus(description=reason))

    def mark_as_superseded(self, description: str, by: StageNumber):
        self._set_status(SupersededStatus(description=description, by=by))

    def publish(self) -> PublishedVersion:
        """mark this staged version candidate as accepted and try to publish it"""
        self._set_status(AcceptedStatus())
        versions = self.get_versions()
        # check status of older staged versions
        for nr, details in versions.staged.items():
            if nr >= self.number:  # ignore newer staged versions
                continue
            if isinstance(details.status, (SupersededStatus, PublishedStagedStatus)):
                pass
            elif isinstance(
                details.status,
                (
                    UnpackingStatus,
                    UnpackedStatus,
                    TestingStatus,
                    AwaitingReviewStatus,
                    ChangesRequestedStatus,
                    AcceptedStatus,
                ),
            ):
                superseded = StagedVersion(client=self.client, id=self.id, number=nr)
                superseded.mark_as_superseded(
                    f"Superseded by {self.number}", self.number
                )
            else:
                assert_never(details.status)

        if not versions.published:
            next_publish_nr = PublishNumber(1)
        else:
            next_publish_nr = PublishNumber(max(versions.published) + 1)

        logger.debug("Publishing {} as version nr {}", self.folder, next_publish_nr)

        # load rdf
        staged_rdf_path = f"{self.folder}files/rdf.yaml"
        rdf_data = self.client.load_file(staged_rdf_path)
        rdf = yaml.load(rdf_data)

        sem_ver = rdf.get("version")
        if sem_ver is not None:
            sem_ver = str(sem_ver)
            if sem_ver in {v.sem_ver for v in versions.published.values()}:
                raise RuntimeError(f"Trying to publish {sem_ver} again!")

        ret = PublishedVersion(client=self.client, id=self.id, number=next_publish_nr)

        # copy rdf.yaml and set version in it
        rdf["version_number"] = ret.number
        stream = io.StringIO()
        yaml.dump(rdf, stream)
        rdf_data = stream.read().encode()
        self.client.put(
            f"{ret.folder}files/rdf.yaml", io.BytesIO(rdf_data), length=len(rdf_data)
        )
        # self.client.rm_obj(staged_rdf_path)

        # move all other files
        self.client.cp_dir(self.folder, ret.folder)

        versions.staged[self.number].status = PublishedStagedStatus(
            publish_number=next_publish_nr
        )
        versions.published[next_publish_nr] = PublishedVersionDetails(
            sem_ver=sem_ver, status=PublishedStatus(stage_number=self.number)
        )
        self._extend_version_agnostic_json(versions)

        # TODO: clean up staged files?
        # remove all uploaded files from this staged version
        # self.client.rm_dir(f"{self.folder}/files/")
        return ret

    def _set_status(self, value: StagedVersionStatus):
        versions = self.get_versions()
        details = versions.staged.setdefault(
            self.number, StagedVersionDetails(status=value)
        )
        if value.step < details.status.step:
            logger.error("Cannot proceed from {} to {}", details.status, value)
            return

        if value.step not in (details.status.step, details.status.step + 1) and not (
            details.status.name == "awaiting review" and value.name == "superseded"
        ):
            logger.warning("Proceeding from {} to {}", details.status, value)

        details.status = value
        self._extend_version_agnostic_json(versions)


@dataclass
class PublishedVersion(RemoteResourceVersion[PublishNumber]):
    """A representation of a published resource version"""

    @property
    def version_prefix(self):
        """published versions do not have a prefix"""
        return ""
