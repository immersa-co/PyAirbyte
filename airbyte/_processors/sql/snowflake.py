# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
"""A Snowflake implementation of the SQL processor."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from textwrap import indent
from typing import TYPE_CHECKING

import sqlalchemy
from overrides import overrides
from pydantic import Field
from snowflake import connector
from snowflake.sqlalchemy import URL, VARIANT
from sqlalchemy import text

from airbyte import exceptions as exc
from airbyte._writers.jsonl import JsonlWriter
from airbyte.constants import DEFAULT_CACHE_SCHEMA_NAME
from airbyte.secrets.base import SecretString
from airbyte.shared import SqlProcessorBase
from airbyte.shared.sql_processor import SqlConfig
from airbyte.types import SQLTypeConverter


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection


MAX_UPLOAD_THREADS = 8

TIMESTAMP_FORMATS = ['AUTO', 'YYYY-MM-DDTHH24:MI:SS.FF9TZHTZM']

class SnowflakeConfig(SqlConfig):
    """Configuration for the Snowflake cache."""

    account: str
    username: str
    private_key: SecretString
    warehouse: str
    database: str
    role: str
    schema_name: str = Field(default=DEFAULT_CACHE_SCHEMA_NAME)
    data_retention_time_in_days: int | None = None

    @overrides
    def get_create_table_extra_clauses(self) -> list[str]:
        """Return a list of clauses to append on CREATE TABLE statements."""
        clauses = []

        if self.data_retention_time_in_days is not None:
            clauses.append(f"DATA_RETENTION_TIME_IN_DAYS = {self.data_retention_time_in_days}")

        return clauses

    @overrides
    def get_database_name(self) -> str:
        """Return the name of the database."""
        return self.database

    @overrides
    def get_sql_alchemy_url(self) -> SecretString:
        """Return the SQLAlchemy URL to use."""
        return SecretString(
            URL(
                account=self.account,
                user=self.username,
                private_key=self.private_key,
                database=self.database,
                warehouse=self.warehouse,
                schema=self.schema_name,
                role=self.role,
            )
        )

    def get_vendor_client(self) -> object:
        """Return the Snowflake connection object."""
        return connector.connect(
            user=self.username,
            private_key=self.private_key,
            account=self.account,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema_name,
            role=self.role,
        )


class SnowflakeTypeConverter(SQLTypeConverter):
    """A class to convert types for Snowflake."""

    @overrides
    def to_sql_type(
        self,
        json_schema_property_def: dict[str, str | dict | list],
    ) -> sqlalchemy.types.TypeEngine:
        """Convert a value to a SQL type.

        We first call the parent class method to get the type. Then if the type JSON, we
        replace it with VARIANT.
        """
        sql_type = super().to_sql_type(json_schema_property_def)
        if isinstance(sql_type, sqlalchemy.types.JSON):
            return VARIANT()

        return sql_type

    @staticmethod
    def get_json_type() -> sqlalchemy.types.TypeEngine:
        """Get the type to use for nested JSON data."""
        return VARIANT()


class SnowflakeSqlProcessor(SqlProcessorBase):
    """A Snowflake implementation of the cache."""

    file_writer_class = JsonlWriter
    type_converter_class: type[SnowflakeTypeConverter] = SnowflakeTypeConverter
    supports_merge_insert = True
    sql_config: SnowflakeConfig

    @overrides
    def _write_files_to_new_table(
        self,
        files: list[Path],
        stream_name: str,
        batch_id: str,
    ) -> str:
        """Write files to a new table."""
        temp_table_name = self._create_table_for_loading(
            stream_name=stream_name,
            batch_id=batch_id,
        )
        internal_sf_stage_name = f"@%{temp_table_name}"

        def path_str(path: Path) -> str:
            return str(path.absolute()).replace("\\", "\\\\")

        def upload_file(file_path: Path) -> None:
            query = f"PUT 'file://{path_str(file_path)}' {internal_sf_stage_name};"
            self._execute_sql(query)

        # Upload files in parallel
        with ThreadPoolExecutor(max_workers=MAX_UPLOAD_THREADS) as executor:
            try:
                executor.map(upload_file, files)
            except Exception as e:
                raise exc.PyAirbyteInternalError(
                    message="Failed to upload batch files to Snowflake.",
                    context={"files": [str(f) for f in files]},
                ) from e

        files_list = ", ".join([f"'{f.name}'" for f in files])

        columns =  self._get_sql_column_definitions(stream_name)

        columns_list = [self._quote_identifier(c) for c in list(columns.keys())]
        columns_list_str: str = indent("\n, ".join(columns_list), " " * 12)

        def _clause(_col, _dtype):
            c = f"$1:{self._quote_identifier(_col)}"
            clause = (
                "COALESCE( " +
                ",".join([f"TRY_TO_TIMESTAMP({c}::STRING, '{f}')" for f in TIMESTAMP_FORMATS]) +
                " )"
            ) if isinstance(_dtype, sqlalchemy.types.TIMESTAMP) else c
            return clause

        columns_list = [_clause(_col, _dtype) for _col, _dtype in columns.items()]
        variant_cols_str: str = indent("\n, ".join(columns_list), " " * 21)

        copy_statement = f"""
            COPY INTO {temp_table_name}
            (
                {columns_list_str}
            )
            FROM (
                SELECT {variant_cols_str}
                FROM {internal_sf_stage_name}
            )
            FILES = ( {files_list} )
            FILE_FORMAT = ( TYPE = JSON, COMPRESSION = GZIP )
            ;
            """
        self._execute_sql(text(copy_statement))
        return temp_table_name

    @overrides
    def _init_connection_settings(self, connection: Connection) -> None:
        """We set Snowflake-specific settings for the session.

        This sets QUOTED_IDENTIFIERS_IGNORE_CASE setting to True, which is necessary because
        Snowflake otherwise will treat quoted table and column references as case-sensitive.
        More info: https://docs.snowflake.com/en/sql-reference/identifiers-syntax

        This also sets MULTI_STATEMENT_COUNT to 0, which allows multi-statement commands.
        """
        connection.execute(
            text(
                """
                ALTER SESSION SET
                QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE
                MULTI_STATEMENT_COUNT = 0
                """
            )
        )
