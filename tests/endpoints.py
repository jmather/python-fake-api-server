"""Endpoint fixtures shared with the Node and .NET suites."""


def _plain(status, content, delay=0, headers=None):
    response = {
        "status": status,
        "content": content,
        "content_type": "text/plain",
        "delay": delay,
    }
    if headers is not None:
        response["headers"] = headers
    return response


ENDPOINTS = {
    "bad_hello_world": {
        "method": "get",
        "path": "/",
        "responses": [_plain(200, "Hello World!")],
    },
    "hello_world": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "responses": [_plain(200, "Hello World!")],
    },
    "post_hello_world": {
        "method": "post",
        "path": "/hello-world",
        "response_mode": "incremental",
        "responses": [_plain(201, "Hello World!")],
    },
    "post_hello_world_body": {
        "method": "post",
        "path": "/hello-world-body",
        "body": "Foo",
        "response_mode": "incremental",
        "responses": [_plain(201, "Hello World!")],
    },
    "put_hello_world": {
        "method": "put",
        "path": "/hello-world",
        "response_mode": "incremental",
        "responses": [_plain(201, "Hello World!")],
    },
    "hello_incremental": {
        "method": "get",
        "path": "/hello-incremental",
        "response_mode": "incremental",
        "responses": [_plain(200, "Hello Bob!"), _plain(200, "Hello Bill!")],
    },
    "hello_delayed": {
        "method": "get",
        "path": "/hello-delayed",
        "response_mode": "incremental",
        "responses": [_plain(200, "Hello delayed!", delay=200)],
    },
    "hello_random": {
        "method": "get",
        "path": "/hello-random",
        "response_mode": "random",
        "responses": [_plain(200, f"Hello {name}!") for name in "ABCDE"],
    },
    "hello_foo": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "query_parameters": {"who": "foo"},
        "responses": [_plain(200, "Hello Foo!")],
    },
    "hello_foo_other": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "query_parameters": {"who": "foo", "other": "other"},
        "responses": [_plain(200, "Hello Other Foo!")],
    },
    "hello_foo_other2": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "query_parameters": {"who": "fizzle", "other": "other"},
        "responses": [_plain(200, "Hello Other Foo!")],
    },
    "hello_bar": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "headers": {"x-whom": "bar"},
        "responses": [_plain(200, "Hello Bar!")],
    },
    "hello_bar_other": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "headers": {"x-whom": "bar", "x-other": "other"},
        "responses": [_plain(200, "Hello Other Bar!")],
    },
    "hello_bar_more": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "headers": {"x-whom": "bar", "x-more": "yes please"},
        "responses": [_plain(200, "Hello Bar!")],
    },
    "hello_bar_more2": {
        "method": "get",
        "path": "/hello-world",
        "response_mode": "incremental",
        "headers": {"x-whom": "boo", "x-more": "yes please"},
        "responses": [_plain(200, "Hello Bar!")],
    },
    "header_response": {
        "method": "get",
        "path": "/respond-with-headers",
        "response_mode": "incremental",
        "responses": [_plain(200, "Now with headers!", headers={"X-Foo": "bar"})],
    },
}
