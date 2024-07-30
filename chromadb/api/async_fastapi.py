import asyncio
from uuid import UUID
import urllib.parse
import orjson
from typing import Any, Optional, cast, Tuple, Sequence, Dict
import logging
import httpx
from overrides import override
from chromadb.api.async_api import AsyncServerAPI
from chromadb.api.base_http_client import BaseHTTPClient
from chromadb.api.configuration import CollectionConfigurationInternal
from chromadb.config import DEFAULT_DATABASE, DEFAULT_TENANT, System, Settings
from chromadb.telemetry.opentelemetry import (
    OpenTelemetryClient,
    OpenTelemetryGranularity,
    trace_method,
)
from chromadb.telemetry.product import ProductTelemetryClient
from chromadb.utils.async_to_sync import async_to_sync

from chromadb.types import Database, Tenant, Collection as CollectionModel

from chromadb.api.types import (
    Documents,
    Embeddings,
    PyEmbeddings,
    IDs,
    Include,
    Metadatas,
    URIs,
    Where,
    WhereDocument,
    GetResult,
    QueryResult,
    CollectionMetadata,
    validate_batch,
    convert_np_embeddings_to_list,
)


logger = logging.getLogger(__name__)


class AsyncFastAPI(BaseHTTPClient, AsyncServerAPI):
    # We make one client per event loop to avoid unexpected issues if a client
    # is shared between event loops.
    # For example, if a client is constructed in the main thread, then passed
    # (or a returned Collection is passed) to a new thread, the client would
    # normally throw an obscure asyncio error.
    # Mixing asyncio and threading in this manner usually discouraged, but
    # this gives a better user experience with practically no downsides.
    # https://github.com/encode/httpx/issues/2058
    _clients: Dict[int, httpx.AsyncClient] = {}

    def __init__(self, system: System):
        super().__init__(system)

        system.settings.require("chroma_server_host")
        system.settings.require("chroma_server_http_port")

        self._opentelemetry_client = self.require(OpenTelemetryClient)
        self._product_telemetry_client = self.require(ProductTelemetryClient)
        self._settings = system.settings

        self._api_url = AsyncFastAPI.resolve_url(
            chroma_server_host=str(system.settings.chroma_server_host),
            chroma_server_http_port=system.settings.chroma_server_http_port,
            chroma_server_ssl_enabled=system.settings.chroma_server_ssl_enabled,
            default_api_path=system.settings.chroma_server_api_default_path,
        )

    async def __aenter__(self) -> "AsyncFastAPI":
        self._get_client()
        return self

    async def _cleanup(self) -> None:
        while len(self._clients) > 0:
            (_, client) = self._clients.popitem()
            await client.aclose()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self._cleanup()

    @override
    def start(self) -> None:
        super().start()

    @override
    def stop(self) -> None:
        super().stop()

        @async_to_sync
        async def sync_cleanup() -> None:
            await self._cleanup()

        sync_cleanup()

    def _get_client(self) -> httpx.AsyncClient:
        # Ideally this would use anyio to be compatible with both
        # asyncio and trio, but anyio does not expose any way to identify
        # the current event loop.
        # We attempt to get the loop assuming the environment is asyncio, and
        # otherwise gracefully fall back to using a singleton client.
        loop_hash = None
        try:
            loop = asyncio.get_event_loop()
            loop_hash = loop.__hash__()
        except RuntimeError:
            loop_hash = 0

        if loop_hash not in self._clients:
            self._clients[loop_hash] = httpx.AsyncClient(timeout=None)

        return self._clients[loop_hash]

    async def _make_request(
        self, method: str, path: str, **kwargs: Dict[str, Any]
    ) -> Any:
        # If the request has json in kwargs, use orjson to serialize it,
        # remove it from kwargs, and add it to the content parameter
        # This is because httpx uses a slower json serializer
        if "json" in kwargs:
            data = orjson.dumps(kwargs.pop("json"))
            kwargs["content"] = data

        # Unlike requests, httpx does not automatically escape the path
        escaped_path = urllib.parse.quote(path, safe="/", encoding=None, errors=None)
        url = self._api_url + escaped_path

        response = await self._get_client().request(method, url, **cast(Any, kwargs))
        BaseHTTPClient._raise_chroma_error(response)
        return orjson.loads(response.text)

    @trace_method("AsyncFastAPI.heartbeat", OpenTelemetryGranularity.OPERATION)
    @override
    async def heartbeat(self) -> int:
        self._raise_for_running()
        response = await self._make_request("get", "")
        return int(response["nanosecond heartbeat"])

    @trace_method("AsyncFastAPI.create_database", OpenTelemetryGranularity.OPERATION)
    @override
    async def create_database(
        self,
        name: str,
        tenant: str = DEFAULT_TENANT,
    ) -> None:
        self._raise_for_running()
        await self._make_request(
            "post",
            "/databases",
            json={"name": name},
            params={"tenant": tenant},
        )

    @trace_method("AsyncFastAPI.get_database", OpenTelemetryGranularity.OPERATION)
    @override
    async def get_database(
        self,
        name: str,
        tenant: str = DEFAULT_TENANT,
    ) -> Database:
        self._raise_for_running()
        response = await self._make_request(
            "get",
            "/databases/" + name,
            params={"tenant": tenant},
        )

        return Database(
            id=response["id"], name=response["name"], tenant=response["tenant"]
        )

    @trace_method("AsyncFastAPI.create_tenant", OpenTelemetryGranularity.OPERATION)
    @override
    async def create_tenant(self, name: str) -> None:
        self._raise_for_running()
        await self._make_request(
            "post",
            "/tenants",
            json={"name": name},
        )

    @trace_method("AsyncFastAPI.get_tenant", OpenTelemetryGranularity.OPERATION)
    @override
    async def get_tenant(self, name: str) -> Tenant:
        self._raise_for_running()
        resp_json = await self._make_request(
            "get",
            "/tenants/" + name,
        )

        return Tenant(name=resp_json["name"])

    @trace_method("AsyncFastAPI.list_collections", OpenTelemetryGranularity.OPERATION)
    @override
    async def list_collections(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> Sequence[CollectionModel]:
        self._raise_for_running()
        resp_json = await self._make_request(
            "get",
            "/collections",
            params=BaseHTTPClient._clean_params(
                {
                    "tenant": tenant,
                    "database": database,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )

        models = [
            CollectionModel.from_json(json_collection) for json_collection in resp_json
        ]
        return models

    @trace_method("AsyncFastAPI.count_collections", OpenTelemetryGranularity.OPERATION)
    @override
    async def count_collections(
        self, tenant: str = DEFAULT_TENANT, database: str = DEFAULT_DATABASE
    ) -> int:
        self._raise_for_running()
        resp_json = await self._make_request(
            "get",
            "/count_collections",
            params={"tenant": tenant, "database": database},
        )

        return cast(int, resp_json)

    @trace_method("AsyncFastAPI.create_collection", OpenTelemetryGranularity.OPERATION)
    @override
    async def create_collection(
        self,
        name: str,
        configuration: Optional[CollectionConfigurationInternal] = None,
        metadata: Optional[CollectionMetadata] = None,
        get_or_create: bool = False,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> CollectionModel:
        """Creates a collection"""
        self._raise_for_running()
        resp_json = await self._make_request(
            "post",
            "/collections",
            json={
                "name": name,
                "metadata": metadata,
                "configuration": configuration.to_json() if configuration else None,
                "get_or_create": get_or_create,
            },
            params={"tenant": tenant, "database": database},
        )

        model = CollectionModel.from_json(resp_json)

        return model

    @trace_method("AsyncFastAPI.get_collection", OpenTelemetryGranularity.OPERATION)
    @override
    async def get_collection(
        self,
        name: str,
        id: Optional[UUID] = None,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> CollectionModel:
        self._raise_for_running()
        if (name is None and id is None) or (name is not None and id is not None):
            raise ValueError("Name or id must be specified, but not both")

        params = {"tenant": tenant, "database": database}
        if id is not None:
            params["type"] = str(id)

        resp_json = await self._make_request(
            "get",
            "/collections/" + name if name else str(id),
            params=params,
        )

        model = CollectionModel.from_json(resp_json)

        return model

    @trace_method(
        "AsyncFastAPI.get_or_create_collection", OpenTelemetryGranularity.OPERATION
    )
    @override
    async def get_or_create_collection(
        self,
        name: str,
        configuration: Optional[CollectionConfigurationInternal] = None,
        metadata: Optional[CollectionMetadata] = None,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> CollectionModel:
        self._raise_for_running()
        return await self.create_collection(
            name=name,
            configuration=configuration,
            metadata=metadata,
            get_or_create=True,
            tenant=tenant,
            database=database,
        )

    @trace_method("AsyncFastAPI._modify", OpenTelemetryGranularity.OPERATION)
    @override
    async def _modify(
        self,
        id: UUID,
        new_name: Optional[str] = None,
        new_metadata: Optional[CollectionMetadata] = None,
    ) -> None:
        self._raise_for_running()
        await self._make_request(
            "put",
            "/collections/" + str(id),
            json={"new_metadata": new_metadata, "new_name": new_name},
        )

    @trace_method("AsyncFastAPI.delete_collection", OpenTelemetryGranularity.OPERATION)
    @override
    async def delete_collection(
        self,
        name: str,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> None:
        self._raise_for_running()
        await self._make_request(
            "delete",
            "/collections/" + name,
            params={"tenant": tenant, "database": database},
        )

    @trace_method("AsyncFastAPI._count", OpenTelemetryGranularity.OPERATION)
    @override
    async def _count(
        self,
        collection_id: UUID,
    ) -> int:
        """Returns the number of embeddings in the database"""
        self._raise_for_running()
        resp_json = await self._make_request(
            "get",
            "/collections/" + str(collection_id) + "/count",
        )

        return cast(int, resp_json)

    @trace_method("AsyncFastAPI._peek", OpenTelemetryGranularity.OPERATION)
    @override
    async def _peek(
        self,
        collection_id: UUID,
        n: int = 10,
    ) -> GetResult:
        self._raise_for_running()
        return await self._get(
            collection_id,
            limit=n,
            include=["embeddings", "documents", "metadatas"],  # type: ignore[list-item]
        )

    @trace_method("AsyncFastAPI._get", OpenTelemetryGranularity.OPERATION)
    @override
    async def _get(
        self,
        collection_id: UUID,
        ids: Optional[IDs] = None,
        where: Optional[Where] = {},
        sort: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        where_document: Optional[WhereDocument] = {},
        include: Include = ["metadatas", "documents"],  # type: ignore[list-item]
    ) -> GetResult:
        self._raise_for_running()
        if page and page_size:
            offset = (page - 1) * page_size
            limit = page_size

        resp_json = await self._make_request(
            "post",
            "/collections/" + str(collection_id) + "/get",
            json={
                "ids": ids,
                "where": where,
                "sort": sort,
                "limit": limit,
                "offset": offset,
                "where_document": where_document,
                "include": include,
            },
        )

        return GetResult(
            ids=resp_json["ids"],
            embeddings=resp_json.get("embeddings", None),
            metadatas=resp_json.get("metadatas", None),
            documents=resp_json.get("documents", None),
            data=None,
            uris=resp_json.get("uris", None),
            included=resp_json.get("included", include),
        )

    @trace_method("AsyncFastAPI._delete", OpenTelemetryGranularity.OPERATION)
    @override
    async def _delete(
        self,
        collection_id: UUID,
        ids: Optional[IDs] = None,
        where: Optional[Where] = {},
        where_document: Optional[WhereDocument] = {},
    ) -> None:
        self._raise_for_running()
        await self._make_request(
            "post",
            "/collections/" + str(collection_id) + "/delete",
            json={"where": where, "ids": ids, "where_document": where_document},
        )
        return None

    @trace_method("AsyncFastAPI._submit_batch", OpenTelemetryGranularity.ALL)
    async def _submit_batch(
        self,
        batch: Tuple[
            IDs,
            Optional[PyEmbeddings],
            Optional[Metadatas],
            Optional[Documents],
            Optional[URIs],
        ],
        url: str,
    ) -> Any:
        """
        Submits a batch of embeddings to the database
        """
        self._raise_for_running()
        return await self._make_request(
            "post",
            url,
            json={
                "ids": batch[0],
                "embeddings": batch[1],
                "metadatas": batch[2],
                "documents": batch[3],
                "uris": batch[4],
            },
        )

    @trace_method("AsyncFastAPI._add", OpenTelemetryGranularity.ALL)
    @override
    async def _add(
        self,
        ids: IDs,
        collection_id: UUID,
        embeddings: Embeddings,
        metadatas: Optional[Metadatas] = None,
        documents: Optional[Documents] = None,
        uris: Optional[URIs] = None,
    ) -> bool:
        self._raise_for_running()
        batch = (
            ids,
            convert_np_embeddings_to_list(embeddings),
            metadatas,
            documents,
            uris,
        )
        validate_batch(batch, {"max_batch_size": await self.get_max_batch_size()})
        await self._submit_batch(batch, "/collections/" + str(collection_id) + "/add")
        return True

    @trace_method("AsyncFastAPI._update", OpenTelemetryGranularity.ALL)
    @override
    async def _update(
        self,
        collection_id: UUID,
        ids: IDs,
        embeddings: Optional[Embeddings] = None,
        metadatas: Optional[Metadatas] = None,
        documents: Optional[Documents] = None,
        uris: Optional[URIs] = None,
    ) -> bool:
        self._raise_for_running()
        batch = (
            ids,
            convert_np_embeddings_to_list(embeddings)
            if embeddings is not None
            else None,
            metadatas,
            documents,
            uris,
        )
        validate_batch(batch, {"max_batch_size": await self.get_max_batch_size()})

        await self._submit_batch(
            batch, "/collections/" + str(collection_id) + "/update"
        )

        return True

    @trace_method("AsyncFastAPI._upsert", OpenTelemetryGranularity.ALL)
    @override
    async def _upsert(
        self,
        collection_id: UUID,
        ids: IDs,
        embeddings: Embeddings,
        metadatas: Optional[Metadatas] = None,
        documents: Optional[Documents] = None,
        uris: Optional[URIs] = None,
    ) -> bool:
        self._raise_for_running()
        batch = (
            ids,
            convert_np_embeddings_to_list(embeddings),
            metadatas,
            documents,
            uris,
        )
        validate_batch(batch, {"max_batch_size": await self.get_max_batch_size()})
        await self._submit_batch(
            batch, "/collections/" + str(collection_id) + "/upsert"
        )
        return True

    @trace_method("AsyncFastAPI._query", OpenTelemetryGranularity.ALL)
    @override
    async def _query(
        self,
        collection_id: UUID,
        query_embeddings: Embeddings,
        n_results: int = 10,
        where: Optional[Where] = {},
        where_document: Optional[WhereDocument] = {},
        include: Include = ["metadatas", "documents", "distances"],  # type: ignore[list-item]
    ) -> QueryResult:
        self._raise_for_running()
        resp_json = await self._make_request(
            "post",
            "/collections/" + str(collection_id) + "/query",
            json={
                "query_embeddings": convert_np_embeddings_to_list(query_embeddings)
                if query_embeddings is not None
                else None,
                "n_results": n_results,
                "where": where,
                "where_document": where_document,
                "include": include,
            },
        )

        return QueryResult(
            ids=resp_json["ids"],
            distances=resp_json.get("distances", None),
            embeddings=resp_json.get("embeddings", None),
            metadatas=resp_json.get("metadatas", None),
            documents=resp_json.get("documents", None),
            uris=resp_json.get("uris", None),
            data=None,
            included=resp_json.get("included", include),
        )

    @trace_method("AsyncFastAPI.reset", OpenTelemetryGranularity.ALL)
    @override
    async def reset(self) -> bool:
        self._raise_for_running()
        resp_json = await self._make_request("post", "/reset")
        return cast(bool, resp_json)

    @trace_method("AsyncFastAPI.get_version", OpenTelemetryGranularity.OPERATION)
    @override
    async def get_version(self) -> str:
        self._raise_for_running()
        resp_json = await self._make_request("get", "/version")
        return cast(str, resp_json)

    @override
    def get_settings(self) -> Settings:
        return self._settings

    @trace_method("AsyncFastAPI.get_max_batch_size", OpenTelemetryGranularity.OPERATION)
    @override
    async def get_max_batch_size(self) -> int:
        self._raise_for_running()
        if self._max_batch_size == -1:
            resp_json = await self._make_request("get", "/pre-flight-checks")
            self._max_batch_size = cast(int, resp_json["max_batch_size"])
        return self._max_batch_size

    @trace_method("FastAPI.close", OpenTelemetryGranularity.OPERATION)
    @override
    async def close(self) -> None:
        await self._cleanup()  # this is a bit hacky but when running close in a loop the async to sync cleanup doesn't work
        self.stop()
        self._system.stop()
