"""Classes for communicating with the Somfy UAI+"""

from __future__ import annotations
from async_timeout import timeout
from asyncio import create_task, Event, TimeoutError

import json

import telnetlib3


class TargetInfo:
    def __init__(self, name: str, type: str):
        self.name: str = name
        self.type: str = type


class GroupInfo:
    def __init__(self, name: str):
        self.name: str = name

class InvalidUserException(Exception):
    """Error indicating invalid user specified."""

class InvalidPasswordException(Exception):
    """Error indicating invalid password specified."""

class ReaderClosedException(Exception):
    """Error indicating that the connection was closed."""

USER_PROMPT = "User:"
PASSWORD_PROMPT = "Password:"
CONNECTED_NOTIFICATION = "Connected:\n\x00"


class TelnetClient:
    """Represents a client for communicating with the telnet server of a
    Somfy UAI+."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        async_on_connection_ready,
        async_on_disconnected,
    ):
        self._reader = None
        self._writer = None
        self._host: str = host
        self._user: str = user
        self._password: str = password
        self._user_prompt_received: bool = False
        self._password_prompt_received: bool = False
        self._connection_negotiated: bool = False
        self._connection_negotiation_event: Event = Event()
        self._reader_closed_exception: ReaderClosedException = None
        self._next_request_id: int = 0
        self._responses_by_request_id: dict(int, dict(str, any)) = {}
        self._async_on_connection_ready = async_on_connection_ready
        self._async_on_disconnected = async_on_disconnected
        self._read_loop_finished: Event = Event()

    async def async_connect(self) -> None:
        """Connects to the telnet server and reads data on the async
        event loop."""
        self._user_prompt_received = False
        self._password_prompt_received = False
        self._connection_negotiated = False
        self._connection_negotiation_event.clear()
        self._reader_closed_exception = None
        self._next_request_id = 0
        self._responses_by_request_id = {}
        self._read_loop_finished.clear()

        try:
            async with timeout(5):
                self._reader, self._writer = await telnetlib3.open_connection(
                    self._host,
                    connect_minwait=0.0,
                    connect_maxwait=0.0,
                    shell=self._read_loop,
                )
        except (TimeoutError, OSError) as exc:
            raise ConnectionError from exc

    async def async_disconnect(self) -> None:
        """Disconnects from the telnet server."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        await self._read_loop_finished.wait()

    async def async_wait_for_connection_establishment(self) -> None:
        await self._connection_negotiation_event.wait()
        if self._reader_closed_exception is not None:
            raise self._reader_closed_exception

    async def _read_loop(self, reader, writer) -> None:
        """Async loop to read data received from the telnet server;
        sets device state as a result of data received."""

        exception: Exception = None
        output: str = ""
        while True:
            try:
                read_output = await reader.read(1024)
                if not read_output:
                    # EOF
                    break

                # Append new read output to any prior remaining output
                output = output + read_output

                if not self._connection_negotiated:
                    if output.startswith(USER_PROMPT):
                        if self._user_prompt_received:
                            raise InvalidUserException()
                        self._user_prompt_received = True
                        output = output[len(USER_PROMPT) :]
                        await self._async_send_command(self._user, True)
                    elif output.startswith(PASSWORD_PROMPT):
                        if not self._user_prompt_received:
                            raise Exception(
                                "Received Password prompt without User prompt."
                            )
                        if self._password_prompt_received:
                            raise InvalidPasswordException()
                        self._password_prompt_received = True
                        output = output[len(PASSWORD_PROMPT) :]
                        await self._async_send_command(self._password, True)
                    elif output.startswith(CONNECTED_NOTIFICATION):
                        self._connection_negotiated = True
                        output = output[len(CONNECTED_NOTIFICATION) :]
                        await self._async_notify_connection_ready()

                if self._connection_negotiated:
                    # Parse the complete lines from the output
                    output_lines = output.split("\n")
                    output = output_lines[len(output_lines) - 1]
                    if len(output_lines) > 1:
                        output_lines = output_lines[0 : len(output_lines) - 1]
                    else:
                        output_lines = []

                    for output_line in output_lines:
                        json_response: dict = json.loads(output_line)
                        if "result" in json_response:
                            self._set_response(
                                json_response["id"], json_response["result"], None, None
                            )
                        else:
                            self._set_response(
                                json_response["id"], None, json_response["error"], None
                            )

            except Exception as ex:
                create_task(self.async_disconnect())
                exception = ex
                break

        self._connection_negotiated = False
        self._read_loop_finished.set()
        self._reader = None
        self._reader_closed_exception = ReaderClosedException("Connection to the server was closed.", exception)
        await self._async_notify_disconnected()
        for request_id in list(self._responses_by_request_id.keys()):
            self._set_response(
                request_id, None, None, self._reader_closed_exception
            )

    async def _async_notify_connection_ready(self):
        await self._async_on_connection_ready()
        self._connection_negotiation_event.set()

    async def _async_notify_disconnected(self):
        await self._async_on_disconnected(self._reader_closed_exception)
        self._connection_negotiation_event.set()

    async def _async_send_command(self, command: str, send_now: bool = False) -> None:
        """Sends given command to the server. Automatically appends
        CR to the command string. If connection has not yet been
        negotiated, raises an exception."""
        if self._connection_negotiated or send_now:
            self._writer.write(command + "\r")
            await self._writer.drain()
        else:
            raise Exception("Connection has not been successfully negotiated yet.")

    def _get_next_request_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    def _add_request(self, request_id: int) -> None:
        self._responses_by_request_id[request_id] = {"event": Event()}

    def _set_response(
        self, request_id: int, response: dict(str, any), error_response: any, reader_closed_exception: ReaderClosedException
    ):
        response_data = self._responses_by_request_id.pop(request_id, None)
        if response_data is not None:
            response_data["response"] = response
            response_data["error_response"] = error_response
            response_data["reader_closed_exception"] = reader_closed_exception
            response_data["event"].set()

    async def _async_get_response(self, request_id: int) -> str:
        response_data = self._responses_by_request_id[request_id]
        await response_data["event"].wait()
        if response_data["response"] is not None:
            return response_data["response"]
        elif response_data["error_response"] is not None:
            raise Exception(
                "Error response received: {error}".format(
                    error=response_data["error_response"]
                )
            )
        else:
            raise response_data["reader_closed_exception"]

    async def _async_send_request_and_await_response(
        self, request: str
    ) -> dict(str, any):
        request_id: int = self._get_next_request_id()
        cmd: str = '{{"id": {id}, {request} }}'.format(id=request_id, request=request)
        self._add_request(request_id)
        await self._async_send_command(cmd)
        response = await self._async_get_response(request_id)
        return response

    async def async_get_target_info(self, target_id: str) -> TargetInfo:
        """Retrieves the target's meta information (name and type)"""
        request: str = (
            '"method": "status.info", "params": {{ "targetID": "{target_id}" }}'.format(
                target_id=target_id
            )
        )
        response = await self._async_send_request_and_await_response(request)
        return TargetInfo(response["name"], response["type"])

    async def async_get_target_position(self, target_id: str) -> int:
        """Retrieves the target's position as a percentage (0 fully open; 100 fully closed)"""
        request: str = '"method": "status.position", "params": {{ "targetID": "{target_id}" }}'.format(
            target_id=target_id
        )
        response = await self._async_send_request_and_await_response(request)
        return response

    async def async_get_groups_for_target(self, target_id: str) -> list(str):
        """Retreives the target's group IDs"""
        request: str = (
            '"method": "group.get", "params": {{ "targetID": "{target_id}" }}'.format(
                target_id=target_id
            )
        )
        response = await self._async_send_request_and_await_response(request)
        return response

    async def async_get_group_info(self, group_id: str) -> GroupInfo:
        request: str = (
            '"method": "status.info", "params": {{ "groupID": "{group_id}" }}'.format(
                group_id=group_id
            )
        )
        response = await self._async_send_request_and_await_response(request)
        return GroupInfo(response["name"])

    async def async_move_target_up(self, target_id: str) -> None:
        request: str = (
            '"method": "move.up", "params": {{ "targetID": "{target_id}" }}'.format(
                target_id=target_id
            )
        )
        await self._async_send_request_and_await_response(request)

    async def async_move_target_down(self, target_id: str) -> None:
        request: str = (
            '"method": "move.down", "params": {{ "targetID": "{target_id}" }}'.format(
                target_id=target_id
            )
        )
        await self._async_send_request_and_await_response(request)

    async def async_stop_target(self, target_id: str) -> None:
        request: str = (
            '"method": "move.stop", "params": {{ "targetID": "{target_id}" }}'.format(
                target_id=target_id
            )
        )
        await self._async_send_request_and_await_response(request)

    async def async_move_target_to_position(
        self, target_id: str, position: int
    ) -> None:
        request: str = '"method": "move.to", "params": {{ "targetID": "{target_id}", "position": {position} }}'.format(
            target_id=target_id, position=position
        )
        await self._async_send_request_and_await_response(request)

    async def async_move_target_to_intermediate_position(
        self, target_id: str, intermediate_position: int
    ) -> None:
        request: str = '"method": "move.ip", "params": {{ "targetID": "{target_id}", "value": {intermediate_position} }}'.format(
            target_id=target_id, intermediate_position=intermediate_position
        )
        await self._async_send_request_and_await_response(request)

    async def async_move_target_to_next_intermediate_position(
        self, target_id: str
    ) -> None:
        request: str = '"method": "move.ip.next", "params": {{ "targetID": "{target_id}" }}'.format(
            target_id=target_id
        )
        await self._async_send_request_and_await_response(request)

    async def async_move_target_to_previous_intermediate_position(
        self, target_id: str
    ) -> None:
        request: str = '"method": "move.ip.prev", "params": {{ "targetID": "{target_id}" }}'.format(
            target_id=target_id
        )
        await self._async_send_request_and_await_response(request)
