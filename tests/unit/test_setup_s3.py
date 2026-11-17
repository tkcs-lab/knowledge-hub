import boto3
import pytest
from botocore.stub import Stubber

from knowledge_hub.settings import Settings
from knowledge_hub.setup.s3 import S3Step

BUCKET = "kh"


def make(create=False):
    client = boto3.client(
        "s3",
        endpoint_url="http://s3.invalid",
        region_name="garage",
        aws_access_key_id="a",
        aws_secret_access_key="b",
    )
    return S3Step(Settings(s3_bucket=BUCKET), create_bucket=create, client=client), Stubber(client)


def missing(stub):
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)


def probe(stub, fail_at=None):
    import io

    from botocore.response import StreamingBody

    ok = {
        "put_object": {},
        "get_object": {"Body": StreamingBody(io.BytesIO(b"x"), 1)},
        "delete_object": {},
    }
    for method, response in ok.items():
        if method == fail_at:
            stub.add_client_error(method, service_error_code="AccessDenied", http_status_code=403)
            return
        stub.add_response(method, response)


def lines(changes):
    return [c.render() for c in changes]


def test_requires_bucket():
    with pytest.raises(ValueError, match="KH_S3_BUCKET"):
        S3Step(Settings(s3_bucket=None), client=object())


async def test_absent_plan_makes_no_write():
    step, stub = make()
    missing(stub)
    with stub:
        assert lines(await step.plan()) == ["+ bucket kh"]
        stub.assert_no_pending_responses()


async def test_present():
    step, stub = make()
    stub.add_response("head_bucket", {})
    probe(stub)
    with stub:
        assert await step.plan() == []
        stub.assert_no_pending_responses()


async def test_probe_failure():
    step, stub = make()
    stub.add_response("head_bucket", {})
    probe(stub, fail_at="put_object")
    with stub:
        assert lines(await step.plan()) == ["! bucket kh: probe failed AccessDenied"]


async def test_apply_without_flag_refuses():
    step, stub = make(create=False)
    missing(stub)
    with stub:
        out = lines(await step.apply(False))
    assert out == ["! bucket kh: does not exist; create it or pass --create-bucket"]


async def test_apply_with_flag_creates():
    step, stub = make(create=True)
    missing(stub)
    stub.add_response("create_bucket", {}, {"Bucket": BUCKET})
    probe(stub)
    with stub:
        assert lines(await step.apply(False)) == ["+ bucket kh"]
        stub.assert_no_pending_responses()
