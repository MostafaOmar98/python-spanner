# Copyright 2021 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import hashlib
import pickle
import pytest
import time

from google.cloud import spanner_v1
from google.cloud._helpers import UTC

from google.cloud.spanner_dbapi.connection import Connection, connect
from google.cloud.spanner_dbapi.exceptions import ProgrammingError, OperationalError
from google.cloud.spanner_v1 import JsonObject
from google.cloud.spanner_v1 import gapic_version as package_version
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from . import _helpers

DATABASE_NAME = "dbapi-txn"

DDL_STATEMENTS = (
    """CREATE TABLE contacts (
        contact_id INT64,
        first_name STRING(1024),
        last_name STRING(1024),
        email STRING(1024)
    )
    PRIMARY KEY (contact_id)""",
)


@pytest.fixture(scope="session")
def raw_database(shared_instance, database_operation_timeout, not_postgres):
    database_id = _helpers.unique_id("dbapi-txn")
    pool = spanner_v1.BurstyPool(labels={"testcase": "database_api"})
    database = shared_instance.database(
        database_id,
        ddl_statements=DDL_STATEMENTS,
        pool=pool,
    )
    op = database.create()
    op.result(database_operation_timeout)  # raises on failure / timeout.

    yield database

    database.drop()


class TestDbApi:
    @staticmethod
    def clear_table(transaction):
        transaction.execute_update("DELETE FROM contacts WHERE true")

    @pytest.fixture(scope="function")
    def dbapi_database(self, raw_database):
        raw_database.run_in_transaction(self.clear_table)

        yield raw_database

        raw_database.run_in_transaction(self.clear_table)

    @pytest.fixture(autouse=True)
    def init_connection(self, request, shared_instance, dbapi_database):
        if "noautofixt" not in request.keywords:
            self._conn = Connection(shared_instance, dbapi_database)
            self._cursor = self._conn.cursor()
        yield
        if "noautofixt" not in request.keywords:
            self._cursor.close()
            self._conn.close()

    def _execute_common_statements(self, cursor):
        # execute several DML statements within one transaction
        cursor.execute(
            """
                INSERT INTO contacts (contact_id, first_name, last_name, email)
                VALUES (1, 'first-name', 'last-name', 'test.email@domen.ru')
                """
        )
        cursor.execute(
            """
                UPDATE contacts
                SET first_name = 'updated-first-name'
                WHERE first_name = 'first-name'
                """
        )
        cursor.execute(
            """
                UPDATE contacts
                SET email = 'test.email_updated@domen.ru'
                WHERE email = 'test.email@domen.ru'
                """
        )
        return (
            1,
            "updated-first-name",
            "last-name",
            "test.email_updated@domen.ru",
        )

    @pytest.mark.parametrize("client_side", [True, False])
    def test_commit(self, client_side):
        """Test committing a transaction with several statements."""
        updated_row = self._execute_common_statements(self._cursor)
        if client_side:
            self._cursor.execute("""COMMIT""")
        else:
            self._conn.commit()

        # read the resulting data from the database
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()

        assert got_rows == [updated_row]

    @pytest.mark.skip(reason="b/315807641")
    def test_commit_exception(self):
        """Test that if exception during commit method is caught, then
        subsequent operations on same Cursor and Connection object works
        properly."""
        self._execute_common_statements(self._cursor)
        # deleting the session to fail the commit
        self._conn._session.delete()
        try:
            self._conn.commit()
        except Exception:
            pass

        # Testing that the connection and Cursor are in proper state post commit
        # and a new transaction is started
        updated_row = self._execute_common_statements(self._cursor)
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()

        assert got_rows == [updated_row]

    @pytest.mark.skip(reason="b/315807641")
    def test_rollback_exception(self):
        """Test that if exception during rollback method is caught, then
        subsequent operations on same Cursor and Connection object works
        properly."""
        self._execute_common_statements(self._cursor)
        # deleting the session to fail the rollback
        self._conn._session.delete()
        try:
            self._conn.rollback()
        except Exception:
            pass

        # Testing that the connection and Cursor are in proper state post
        # exception in rollback and a new transaction is started
        updated_row = self._execute_common_statements(self._cursor)
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()

        assert got_rows == [updated_row]

    @pytest.mark.skip(reason="b/315807641")
    def test_cursor_execute_exception(self):
        """Test that if exception in Cursor's execute method is caught when
        Connection is not in autocommit mode, then subsequent operations on
        same Cursor and Connection object works properly."""
        updated_row = self._execute_common_statements(self._cursor)
        try:
            self._cursor.execute("SELECT * FROM unknown_table")
        except Exception:
            pass
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()
        assert got_rows == [updated_row]

        # Testing that the connection and Cursor are in proper state post commit
        # and a new transaction is started
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()
        assert got_rows == [updated_row]

    def test_cursor_execute_exception_autocommit(self):
        """Test that if exception in Cursor's execute method is caught when
        Connection is in autocommit mode, then subsequent operations on
        same Cursor and Connection object works properly."""
        self._conn.autocommit = True
        updated_row = self._execute_common_statements(self._cursor)
        try:
            self._cursor.execute("SELECT * FROM unknown_table")
        except Exception:
            pass
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        assert got_rows == [updated_row]

    def test_cursor_execute_exception_begin_client_side(self):
        """Test that if exception in Cursor's execute method is caught when
        beginning a transaction using client side statement, then subsequent
        operations on same Cursor and Connection object works properly."""
        self._conn.autocommit = True
        self._cursor.execute("begin transaction")
        updated_row = self._execute_common_statements(self._cursor)
        try:
            self._cursor.execute("SELECT * FROM unknown_table")
        except Exception:
            pass
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()
        assert got_rows == [updated_row]

        # Testing that the connection and Cursor are in proper state post commit
        self._conn.autocommit = False
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()
        assert got_rows == [updated_row]

    @pytest.mark.noautofixt
    def test_begin_client_side(self, shared_instance, dbapi_database):
        """Test beginning a transaction using client side statement,
        where connection is in autocommit mode."""

        conn1 = Connection(shared_instance, dbapi_database)
        conn1.autocommit = True
        cursor1 = conn1.cursor()
        cursor1.execute("begin transaction")
        updated_row = self._execute_common_statements(cursor1)

        assert conn1._transaction_begin_marked is True
        conn1.commit()
        assert conn1._transaction_begin_marked is False
        cursor1.close()
        conn1.close()

        # As the connection conn1 is committed a new connection should see its results
        conn3 = Connection(shared_instance, dbapi_database)
        cursor3 = conn3.cursor()
        cursor3.execute("SELECT * FROM contacts")
        conn3.commit()
        got_rows = cursor3.fetchall()
        cursor3.close()
        conn3.close()
        assert got_rows == [updated_row]

    def test_begin_and_commit(self):
        """Test beginning and then committing a transaction is a Noop"""
        self._cursor.execute("begin transaction")
        self._cursor.execute("commit transaction")
        self._cursor.execute("SELECT * FROM contacts")
        self._conn.commit()
        assert self._cursor.fetchall() == []

    def test_begin_and_rollback(self):
        """Test beginning and then rolling back a transaction is a Noop"""
        self._cursor.execute("begin transaction")
        self._cursor.execute("rollback transaction")
        self._cursor.execute("SELECT * FROM contacts")
        self._conn.commit()
        assert self._cursor.fetchall() == []

    def test_read_and_commit_timestamps(self):
        """Test COMMIT_TIMESTAMP is not available after read statement and
        READ_TIMESTAMP is not available after write statement in autocommit
        mode."""
        self._conn.autocommit = True
        self._cursor.execute("SELECT * FROM contacts")
        self._cursor.execute(
            """
            INSERT INTO contacts (contact_id, first_name, last_name, email)
            VALUES (1, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )

        self._cursor.execute("SHOW VARIABLE COMMIT_TIMESTAMP")
        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 1

        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 0

        self._cursor.execute("SELECT * FROM contacts")

        self._cursor.execute("SHOW VARIABLE COMMIT_TIMESTAMP")
        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 0

        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 1

    def test_commit_timestamp_client_side_transaction(self):
        """Test executing SHOW_COMMIT_TIMESTAMP client side statement in a
        transaction."""

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._cursor.execute("SHOW VARIABLE COMMIT_TIMESTAMP")
        got_rows = self._cursor.fetchall()
        # As the connection is not committed we will get 0 rows
        assert len(got_rows) == 0
        assert len(self._cursor.description) == 1

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._conn.commit()
        self._cursor.execute("SHOW VARIABLE COMMIT_TIMESTAMP")

        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 1
        assert len(got_rows[0]) == 1
        assert len(self._cursor.description) == 1
        assert self._cursor.description[0].name == "SHOW_COMMIT_TIMESTAMP"
        assert isinstance(got_rows[0][0], DatetimeWithNanoseconds)

    def test_commit_timestamp_client_side_autocommit(self):
        """Test executing SHOW_COMMIT_TIMESTAMP client side statement in a
        transaction when connection is in autocommit mode."""

        self._conn.autocommit = True
        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._cursor.execute("SHOW VARIABLE COMMIT_TIMESTAMP")

        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 1
        assert len(got_rows[0]) == 1
        assert len(self._cursor.description) == 1
        assert self._cursor.description[0].name == "SHOW_COMMIT_TIMESTAMP"
        assert isinstance(got_rows[0][0], DatetimeWithNanoseconds)

    def test_read_timestamp_client_side(self):
        """Test executing SHOW_READ_TIMESTAMP client side statement in a
        transaction."""

        self._conn.read_only = True
        self._cursor.execute("SELECT * FROM contacts")
        assert self._cursor.fetchall() == []

        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_1 = self._cursor.fetchall()

        self._cursor.execute("SELECT * FROM contacts")
        assert self._cursor.fetchall() == []

        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_2 = self._cursor.fetchall()

        self._conn.commit()

        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_3 = self._cursor.fetchall()
        assert len(self._cursor.description) == 1
        assert self._cursor.description[0].name == "SHOW_READ_TIMESTAMP"

        assert (
            read_timestamp_query_result_1
            == read_timestamp_query_result_2
            == read_timestamp_query_result_3
        )
        assert len(read_timestamp_query_result_1) == 1
        assert len(read_timestamp_query_result_1[0]) == 1
        assert isinstance(read_timestamp_query_result_1[0][0], DatetimeWithNanoseconds)

        self._cursor.execute("SELECT * FROM contacts")
        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_4 = self._cursor.fetchall()
        self._conn.commit()
        assert read_timestamp_query_result_1 != read_timestamp_query_result_4

    def test_read_timestamp_client_side_autocommit(self):
        """Test executing SHOW_READ_TIMESTAMP client side statement in a
        transaction when connection is in autocommit mode."""

        self._conn.autocommit = True

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._conn.read_only = True
        self._cursor.execute("SELECT * FROM contacts")
        assert self._cursor.fetchall() == [
            (2, "first-name", "last-name", "test.email@domen.ru")
        ]
        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_1 = self._cursor.fetchall()

        assert len(read_timestamp_query_result_1) == 1
        assert len(read_timestamp_query_result_1[0]) == 1
        assert len(self._cursor.description) == 1
        assert self._cursor.description[0].name == "SHOW_READ_TIMESTAMP"
        assert isinstance(read_timestamp_query_result_1[0][0], DatetimeWithNanoseconds)

        self._cursor.execute("SELECT * FROM contacts")
        self._cursor.execute("SHOW VARIABLE READ_TIMESTAMP")
        read_timestamp_query_result_2 = self._cursor.fetchall()
        assert read_timestamp_query_result_1 != read_timestamp_query_result_2

    @pytest.mark.parametrize("auto_commit", [False, True])
    def test_batch_dml(self, auto_commit):
        """Test batch dml."""

        if auto_commit:
            self._conn.autocommit = True
        self._insert_row(1)

        self._cursor.execute("start batch dml")
        self._insert_row(2)
        self._insert_row(3)
        self._cursor.execute("run batch")

        self._insert_row(4)

        # Test starting another dml batch in same transaction works
        self._cursor.execute("start batch dml")
        self._insert_row(5)
        self._insert_row(6)
        self._cursor.execute("run batch")

        if not auto_commit:
            self._conn.commit()

        self._cursor.execute("SELECT * FROM contacts")
        assert (
            self._cursor.fetchall().sort()
            == (
                [
                    (1, "first-name-1", "last-name-1", "test.email@domen.ru"),
                    (2, "first-name-2", "last-name-2", "test.email@domen.ru"),
                    (3, "first-name-3", "last-name-3", "test.email@domen.ru"),
                    (4, "first-name-4", "last-name-4", "test.email@domen.ru"),
                    (5, "first-name-5", "last-name-5", "test.email@domen.ru"),
                    (6, "first-name-6", "last-name-6", "test.email@domen.ru"),
                ]
            ).sort()
        )

        # Test starting another dml batch in same connection post commit works
        self._cursor.execute("start batch dml")
        self._insert_row(7)
        self._insert_row(8)
        self._cursor.execute("run batch")

        self._insert_row(9)

        if not auto_commit:
            self._conn.commit()

        self._cursor.execute("SELECT * FROM contacts")
        assert len(self._cursor.fetchall()) == 9

    def test_abort_batch_dml(self):
        """Test abort batch dml."""

        self._cursor.execute("start batch dml")
        self._insert_row(1)
        self._insert_row(2)
        self._cursor.execute("abort batch")

        self._insert_row(3)
        self._conn.commit()

        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        assert len(got_rows) == 1
        assert got_rows == [(3, "first-name-3", "last-name-3", "test.email@domen.ru")]

    def test_batch_dml_invalid_statements(self):
        """Test batch dml having invalid statements."""

        # Test first statement in batch is invalid
        self._cursor.execute("start batch dml")
        self._cursor.execute(
            """
            INSERT INTO unknown_table (contact_id, first_name, last_name, email)
            VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )
        self._insert_row(1)
        self._insert_row(2)
        with pytest.raises(OperationalError):
            self._cursor.execute("run batch")

        # Test middle statement in batch is invalid
        self._cursor.execute("start batch dml")
        self._insert_row(1)
        self._cursor.execute(
            """
            INSERT INTO unknown_table (contact_id, first_name, last_name, email)
            VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )
        self._insert_row(2)
        with pytest.raises(OperationalError):
            self._cursor.execute("run batch")

        # Test last statement in batch is invalid
        self._cursor.execute("start batch dml")
        self._insert_row(1)
        self._insert_row(2)
        self._cursor.execute(
            """
            INSERT INTO unknown_table (contact_id, first_name, last_name, email)
            VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )
        with pytest.raises(OperationalError):
            self._cursor.execute("run batch")

    def _insert_row(self, i):
        self._cursor.execute(
            f"""
            INSERT INTO contacts (contact_id, first_name, last_name, email)
            VALUES ({i}, 'first-name-{i}', 'last-name-{i}', 'test.email@domen.ru')
            """
        )

    def test_begin_success_post_commit(self):
        """Test beginning a new transaction post commiting an existing transaction
        is possible on a connection, when connection is in autocommit mode."""
        want_row = (2, "first-name", "last-name", "test.email@domen.ru")
        self._conn.autocommit = True
        self._cursor.execute("begin transaction")
        self._cursor.execute(
            """
            INSERT INTO contacts (contact_id, first_name, last_name, email)
            VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )
        self._conn.commit()

        self._cursor.execute("begin transaction")
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()
        assert got_rows == [want_row]

    def test_begin_error_before_commit(self):
        """Test beginning a new transaction before commiting an existing transaction is not possible on a connection, when connection is in autocommit mode."""
        self._conn.autocommit = True
        self._cursor.execute("begin transaction")
        self._cursor.execute(
            """
            INSERT INTO contacts (contact_id, first_name, last_name, email)
            VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
            """
        )

        with pytest.raises(OperationalError):
            self._cursor.execute("begin transaction")

    @pytest.mark.parametrize("client_side", [False, True])
    def test_rollback(self, client_side):
        """Test rollbacking a transaction with several statements."""
        want_row = (2, "first-name", "last-name", "test.email@domen.ru")

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._conn.commit()

        # execute several DMLs with one transaction
        self._cursor.execute(
            """
    UPDATE contacts
    SET first_name = 'updated-first-name'
    WHERE first_name = 'first-name'
    """
        )
        self._cursor.execute(
            """
    UPDATE contacts
    SET email = 'test.email_updated@domen.ru'
    WHERE email = 'test.email@domen.ru'
    """
        )

        if client_side:
            self._cursor.execute("ROLLBACK")
        else:
            self._conn.rollback()

        # read the resulting data from the database
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()
        self._conn.commit()

        assert got_rows == [want_row]

    def test_autocommit_mode_change(self):
        """Test auto committing a transaction on `autocommit` mode change."""
        want_row = (
            2,
            "updated-first-name",
            "last-name",
            "test.email@domen.ru",
        )

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (2, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        self._cursor.execute(
            """
    UPDATE contacts
    SET first_name = 'updated-first-name'
    WHERE first_name = 'first-name'
    """
        )
        self._conn.autocommit = True

        # read the resulting data from the database
        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()

        assert got_rows == [want_row]

    @pytest.mark.noautofixt
    def test_rollback_on_connection_closing(self, shared_instance, dbapi_database):
        """
        When closing a connection all the pending transactions
        must be rollbacked. Testing if it's working this way.
        """
        want_row = (1, "first-name", "last-name", "test.email@domen.ru")
        # connect to the test database
        conn = Connection(shared_instance, dbapi_database)
        cursor = conn.cursor()

        cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@domen.ru')
        """
        )
        conn.commit()

        cursor.execute(
            """
    UPDATE contacts
    SET first_name = 'updated-first-name'
    WHERE first_name = 'first-name'
    """
        )
        conn.close()

        # connect again, as the previous connection is no-op after closing
        conn = Connection(shared_instance, dbapi_database)
        cursor = conn.cursor()

        # read the resulting data from the database
        cursor.execute("SELECT * FROM contacts")
        got_rows = cursor.fetchall()
        conn.commit()

        assert got_rows == [want_row]

        cursor.close()
        conn.close()

    def test_results_checksum(self):
        """Test that results checksum is calculated properly."""

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES
    (1, 'first-name', 'last-name', 'test.email@domen.ru'),
    (2, 'first-name2', 'last-name2', 'test.email2@domen.ru')
        """
        )
        assert len(self._conn._statements) == 1
        self._conn.commit()

        self._cursor.execute("SELECT * FROM contacts")
        got_rows = self._cursor.fetchall()

        assert len(self._conn._statements) == 1
        self._conn.commit()

        checksum = hashlib.sha256()
        checksum.update(pickle.dumps(got_rows[0]))
        checksum.update(pickle.dumps(got_rows[1]))

        assert self._cursor._checksum.checksum.digest() == checksum.digest()

    def test_execute_many(self):
        row_data = [
            (1, "first-name", "last-name", "test.email@example.com"),
            (2, "first-name2", "last-name2", "test.email2@example.com"),
        ]
        self._cursor.executemany(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (%s, %s, %s, %s)
        """,
            row_data,
        )
        self._conn.commit()

        self._cursor.executemany(
            """SELECT * FROM contacts WHERE contact_id = %s""",
            ((1,), (2,)),
        )
        res = self._cursor.fetchall()
        self._conn.commit()

        assert len(res) == len(row_data)
        for found, expected in zip(res, row_data):
            assert found[0] == expected[0]

        # checking that execute() and executemany()
        # results are not mixed together
        self._cursor.execute(
            """
    SELECT * FROM contacts WHERE contact_id = 1
    """,
        )
        res = self._cursor.fetchone()
        self._conn.commit()

        assert res[0] == 1

    @pytest.mark.noautofixt
    def test_DDL_autocommit(self, shared_instance, dbapi_database):
        """Check that DDLs in autocommit mode are immediately executed."""

        try:
            conn = Connection(shared_instance, dbapi_database)
            conn.autocommit = True

            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE Singers (
                    SingerId     INT64 NOT NULL,
                    Name    STRING(1024),
                ) PRIMARY KEY (SingerId)
            """
            )
            conn.close()

            # if previous DDL wasn't committed, the next DROP TABLE
            # statement will fail with a ProgrammingError
            conn = Connection(shared_instance, dbapi_database)
            cur = conn.cursor()

            cur.execute("DROP TABLE Singers")
            conn.commit()
        finally:
            # Delete table
            table = dbapi_database.table("Singers")
            if table.exists():
                op = dbapi_database.update_ddl(["DROP TABLE Singers"])
                op.result()

    def test_ddl_execute_autocommit_true(self, dbapi_database):
        """Check that DDL statement in autocommit mode results in successful
        DDL statement execution for execute method."""

        self._conn.autocommit = True
        self._cursor.execute(
            """
            CREATE TABLE DdlExecuteAutocommit (
                SingerId     INT64 NOT NULL,
                Name    STRING(1024),
            ) PRIMARY KEY (SingerId)
            """
        )
        table = dbapi_database.table("DdlExecuteAutocommit")
        assert table.exists() is True

    def test_ddl_executemany_autocommit_true(self, dbapi_database):
        """Check that DDL statement in autocommit mode results in exception for
        executemany method ."""

        self._conn.autocommit = True
        with pytest.raises(ProgrammingError):
            self._cursor.executemany(
                """
                CREATE TABLE DdlExecuteManyAutocommit (
                    SingerId     INT64 NOT NULL,
                    Name    STRING(1024),
                ) PRIMARY KEY (SingerId)
                """,
                [],
            )
        table = dbapi_database.table("DdlExecuteManyAutocommit")
        assert table.exists() is False

    def test_ddl_executemany_autocommit_false(self, dbapi_database):
        """Check that DDL statement in non-autocommit mode results in exception for
        executemany method ."""
        with pytest.raises(ProgrammingError):
            self._cursor.executemany(
                """
                CREATE TABLE DdlExecuteManyAutocommit (
                    SingerId     INT64 NOT NULL,
                    Name    STRING(1024),
                ) PRIMARY KEY (SingerId)
                """,
                [],
            )
        table = dbapi_database.table("DdlExecuteManyAutocommit")
        assert table.exists() is False

    def test_ddl_execute(self, dbapi_database):
        """Check that DDL statement followed by non-DDL execute statement in
        non autocommit mode results in successful DDL statement execution."""

        want_row = (
            1,
            "first-name",
        )
        self._cursor.execute(
            """
            CREATE TABLE DdlExecute (
                SingerId     INT64 NOT NULL,
                Name    STRING(1024),
            ) PRIMARY KEY (SingerId)
            """
        )
        table = dbapi_database.table("DdlExecute")
        assert table.exists() is False

        self._cursor.execute(
            """
            INSERT INTO DdlExecute (SingerId, Name)
            VALUES (1, "first-name")
            """
        )
        assert table.exists() is True
        self._conn.commit()

        # read the resulting data from the database
        self._cursor.execute("SELECT * FROM DdlExecute")
        got_rows = self._cursor.fetchall()

        assert got_rows == [want_row]

    def test_ddl_executemany(self, dbapi_database):
        """Check that DDL statement followed by non-DDL executemany statement in
        non autocommit mode results in successful DDL statement execution."""

        want_row = (
            1,
            "first-name",
        )
        self._cursor.execute(
            """
            CREATE TABLE DdlExecuteMany (
                SingerId     INT64 NOT NULL,
                Name    STRING(1024),
            ) PRIMARY KEY (SingerId)
            """
        )
        table = dbapi_database.table("DdlExecuteMany")
        assert table.exists() is False

        self._cursor.executemany(
            """
            INSERT INTO DdlExecuteMany (SingerId, Name)
            VALUES (%s, %s)
            """,
            [want_row],
        )
        assert table.exists() is True
        self._conn.commit()

        # read the resulting data from the database
        self._cursor.execute("SELECT * FROM DdlExecuteMany")
        got_rows = self._cursor.fetchall()

        assert got_rows == [want_row]

    @pytest.mark.skipif(_helpers.USE_EMULATOR, reason="Emulator does not support json.")
    def test_autocommit_with_json_data(self, dbapi_database):
        """
        Check that DDLs in autocommit mode are immediately
        executed for json fields.
        """
        try:
            self._conn.autocommit = True
            self._cursor.execute(
                """
                CREATE TABLE JsonDetails (
                    DataId     INT64 NOT NULL,
                    Details    JSON,
                ) PRIMARY KEY (DataId)
            """
            )

            # Insert data to table
            self._cursor.execute(
                sql="INSERT INTO JsonDetails (DataId, Details) VALUES (%s, %s)",
                args=(123, JsonObject({"name": "Jakob", "age": "26"})),
            )

            # Read back the data.
            self._cursor.execute("""select * from JsonDetails;""")
            got_rows = self._cursor.fetchall()

            # Assert the response
            assert len(got_rows) == 1
            assert got_rows[0][0] == 123
            assert got_rows[0][1] == {"age": "26", "name": "Jakob"}

            # Drop the table
            self._cursor.execute("DROP TABLE JsonDetails")
            self._conn.commit()
        finally:
            # Delete table
            table = dbapi_database.table("JsonDetails")
            if table.exists():
                op = dbapi_database.update_ddl(["DROP TABLE JsonDetails"])
                op.result()

    @pytest.mark.skipif(_helpers.USE_EMULATOR, reason="Emulator does not support json.")
    def test_json_array(self, dbapi_database):
        try:
            # Create table
            self._conn.autocommit = True

            self._cursor.execute(
                """
                CREATE TABLE JsonDetails (
                    DataId     INT64 NOT NULL,
                    Details    JSON,
                ) PRIMARY KEY (DataId)
            """
            )
            self._cursor.execute(
                "INSERT INTO JsonDetails (DataId, Details) VALUES (%s, %s)",
                [1, JsonObject([1, 2, 3])],
            )

            self._cursor.execute("SELECT * FROM JsonDetails WHERE DataId = 1")
            row = self._cursor.fetchone()
            assert isinstance(row[1], JsonObject)
            assert row[1].serialize() == "[1,2,3]"

            self._cursor.execute("DROP TABLE JsonDetails")
        finally:
            # Delete table
            table = dbapi_database.table("JsonDetails")
            if table.exists():
                op = dbapi_database.update_ddl(["DROP TABLE JsonDetails"])
                op.result()

    @pytest.mark.noautofixt
    def test_DDL_commit(self, shared_instance, dbapi_database):
        """Check that DDLs in commit mode are executed on calling `commit()`."""
        try:
            conn = Connection(shared_instance, dbapi_database)
            cur = conn.cursor()

            cur.execute(
                """
            CREATE TABLE Singers (
                SingerId     INT64 NOT NULL,
                Name    STRING(1024),
            ) PRIMARY KEY (SingerId)
            """
            )
            conn.commit()
            conn.close()

            # if previous DDL wasn't committed, the next DROP TABLE
            # statement will fail with a ProgrammingError
            conn = Connection(shared_instance, dbapi_database)
            cur = conn.cursor()

            cur.execute("DROP TABLE Singers")
            conn.commit()
        finally:
            # Delete table
            table = dbapi_database.table("Singers")
            if table.exists():
                op = dbapi_database.update_ddl(["DROP TABLE Singers"])
                op.result()

    def test_ping(self):
        """Check connection validation method."""
        self._conn.validate()

    @pytest.mark.noautofixt
    def test_user_agent(self, shared_instance, dbapi_database):
        """Check that DB API uses an appropriate user agent."""
        conn = connect(shared_instance.name, dbapi_database.name)
        assert (
            conn.instance._client._client_info.user_agent
            == "gl-dbapi/" + package_version.__version__
        )
        assert (
            conn.instance._client._client_info.client_library_version
            == package_version.__version__
        )

    def test_read_only(self):
        """
        Check that connection set to `read_only=True` uses
        ReadOnly transactions.
        """

        self._conn.read_only = True
        self._cursor.execute("SELECT * FROM contacts")
        assert self._cursor.fetchall() == []
        self._conn.commit()

    def test_read_only_dml(self):
        """
        Check that connection set to `read_only=True` leads to exception when
        executing dml statements.
        """

        self._conn.read_only = True
        with pytest.raises(ProgrammingError):
            self._cursor.execute(
                """
    UPDATE contacts
    SET first_name = 'updated-first-name'
    WHERE first_name = 'first-name'
    """
            )

    def test_staleness(self):
        """Check the DB API `staleness` option."""

        before_insert = datetime.datetime.utcnow().replace(tzinfo=UTC)
        time.sleep(0.25)

        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@example.com')
        """
        )
        self._conn.commit()

        self._conn.read_only = True
        self._conn.staleness = {"read_timestamp": before_insert}
        self._cursor.execute("SELECT * FROM contacts")
        self._conn.commit()
        assert len(self._cursor.fetchall()) == 0

        self._conn.staleness = None
        self._cursor.execute("SELECT * FROM contacts")
        self._conn.commit()
        assert len(self._cursor.fetchall()) == 1

    @pytest.mark.parametrize("autocommit", [False, True])
    def test_rowcount(self, dbapi_database, autocommit):
        try:
            self._conn.autocommit = autocommit

            self._cursor.execute(
                """
            CREATE TABLE Singers (
                SingerId INT64 NOT NULL,
                Name     STRING(1024),
            ) PRIMARY KEY (SingerId)
            """
            )
            self._conn.commit()

            # executemany sets rowcount to the total modified rows
            rows = [(i, f"Singer {i}") for i in range(100)]
            self._cursor.executemany(
                "INSERT INTO Singers (SingerId, Name) VALUES (%s, %s)", rows[:98]
            )
            assert self._cursor.rowcount == 98

            # execute with INSERT
            self._cursor.execute(
                "INSERT INTO Singers (SingerId, Name) VALUES (%s, %s), (%s, %s)",
                [x for row in rows[98:] for x in row],
            )
            assert self._cursor.rowcount == 2

            # execute with UPDATE
            self._cursor.execute("UPDATE Singers SET Name = 'Cher' WHERE SingerId < 25")
            assert self._cursor.rowcount == 25

            # execute with SELECT
            self._cursor.execute("SELECT Name FROM Singers WHERE SingerId < 75")
            assert len(self._cursor.fetchall()) == 75
            # rowcount is not available for SELECT
            assert self._cursor.rowcount == -1

            # execute with DELETE
            self._cursor.execute("DELETE FROM Singers")
            assert self._cursor.rowcount == 100

            # execute with UPDATE matching 0 rows
            self._cursor.execute("UPDATE Singers SET Name = 'Cher' WHERE SingerId < 25")
            assert self._cursor.rowcount == 0

            self._conn.commit()
            self._cursor.execute("DROP TABLE Singers")
            self._conn.commit()
        finally:
            # Delete table
            table = dbapi_database.table("Singers")
            if table.exists():
                op = dbapi_database.update_ddl(["DROP TABLE Singers"])
                op.result()

    @pytest.mark.parametrize("autocommit", [False, True])
    @pytest.mark.skipif(
        _helpers.USE_EMULATOR, reason="Emulator does not support DML Returning."
    )
    def test_dml_returning_insert(self, autocommit):
        self._conn.autocommit = autocommit
        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@example.com')
    THEN RETURN contact_id, first_name
        """
        )
        assert self._cursor.fetchone() == (1, "first-name")
        assert self._cursor.rowcount == 1
        self._conn.commit()

    @pytest.mark.parametrize("autocommit", [False, True])
    @pytest.mark.skipif(
        _helpers.USE_EMULATOR, reason="Emulator does not support DML Returning."
    )
    def test_dml_returning_update(self, autocommit):
        self._conn.autocommit = autocommit
        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@example.com')
        """
        )
        assert self._cursor.rowcount == 1
        self._cursor.execute(
            """
    UPDATE contacts SET first_name = 'new-name' WHERE contact_id = 1
    THEN RETURN contact_id, first_name
        """
        )
        assert self._cursor.fetchone() == (1, "new-name")
        assert self._cursor.rowcount == 1
        self._conn.commit()

    @pytest.mark.parametrize("autocommit", [False, True])
    @pytest.mark.skipif(
        _helpers.USE_EMULATOR, reason="Emulator does not support DML Returning."
    )
    def test_dml_returning_delete(self, autocommit):
        self._conn.autocommit = autocommit
        self._cursor.execute(
            """
    INSERT INTO contacts (contact_id, first_name, last_name, email)
    VALUES (1, 'first-name', 'last-name', 'test.email@example.com')
        """
        )
        assert self._cursor.rowcount == 1
        self._cursor.execute(
            """
    DELETE FROM contacts WHERE contact_id = 1
    THEN RETURN contact_id, first_name
        """
        )
        assert self._cursor.fetchone() == (1, "first-name")
        assert self._cursor.rowcount == 1
        self._conn.commit()
