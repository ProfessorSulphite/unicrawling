"""
Unit tests for edmetriX catalog ingestion uploader (src/uploader.py)
"""
import json
import pytest
import requests
from unittest.mock import patch, MagicMock

from src.uploader import upload_catalog, DEFAULT_EDMETRIX_API_URL


@pytest.fixture
def sample_json_file(tmp_path):
    f = tmp_path / "university.json"
    f.write_text(json.dumps({"main_info": {"name": "Test University"}}), encoding="utf-8")
    return f


@pytest.fixture
def sample_jsonl_file(tmp_path):
    f = tmp_path / "universities.jsonl"
    f.write_text(
        json.dumps({"main_info": {"name": "Uni 1"}}) + "\n" +
        json.dumps({"main_info": {"name": "Uni 2"}}) + "\n",
        encoding="utf-8"
    )
    return f


def test_upload_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        upload_catalog(tmp_path / "non_existent.json", api_key="secret", exit_on_error=False)


def test_upload_missing_key(sample_json_file, monkeypatch):
    monkeypatch.delenv("EDMETRIX_API_KEY", raising=False)
    monkeypatch.delenv("DEV_ACCESS_KEY", raising=False)
    with pytest.raises(ValueError, match="Ingestion key missing"):
        upload_catalog(sample_json_file, api_key="", exit_on_error=False)


def test_upload_json_success(sample_json_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "summary": {
            "countries": ["Pakistan"],
            "universities_created": 1,
            "universities_updated": 0,
            "programs_created": 10,
            "programs_updated": 0,
            "faculties_upserted": 2,
            "programs_deactivated": 0,
            "warning_count": 0,
        }
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = upload_catalog(
            sample_json_file,
            api_url="https://api.test.com/v1",
            api_key="my_secret_key",
            dry_run=False,
            timeout=60,
            exit_on_error=False,
        )

        assert res["summary"]["universities_created"] == 1
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.test.com/v1/dev/catalog/ingest"
        assert kwargs["headers"]["Authorization"] == "Bearer my_secret_key"
        assert kwargs["params"] == {}
        assert kwargs["timeout"] == 60
        assert kwargs["json"] == {"main_info": {"name": "Test University"}}


def test_upload_jsonl_dry_run(sample_jsonl_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "summary": {
            "countries": ["Pakistan"],
            "universities_created": 2,
            "universities_updated": 0,
            "programs_created": 20,
            "programs_updated": 0,
            "faculties_upserted": 4,
            "programs_deactivated": 0,
            "warning_count": 0,
        }
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = upload_catalog(
            sample_jsonl_file,
            api_key="my_secret_key",
            dry_run=True,
            exit_on_error=False,
        )

        assert res["summary"]["universities_created"] == 2
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == f"{DEFAULT_EDMETRIX_API_URL}/dev/catalog/ingest"
        assert kwargs["params"] == {"dry_run": "true"}
        assert len(kwargs["json"]) == 2


def test_upload_env_fallback(sample_json_file, monkeypatch):
    monkeypatch.setenv("DEV_ACCESS_KEY", "env_dev_key")
    monkeypatch.setenv("EDMETRIX_API_URL", "https://custom.backend.com/api/v1")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"summary": {}}

    with patch("requests.post", return_value=mock_resp) as mock_post:
        upload_catalog(sample_json_file, exit_on_error=False)
        args, kwargs = mock_post.call_args
        assert args[0] == "https://custom.backend.com/api/v1/dev/catalog/ingest"
        assert kwargs["headers"]["Authorization"] == "Bearer env_dev_key"


def test_upload_auth_error(sample_json_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(PermissionError, match="Authentication Failed"):
            upload_catalog(sample_json_file, api_key="bad_key", exit_on_error=False)


def test_upload_server_error(sample_json_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Ingestion Failed"):
            upload_catalog(sample_json_file, api_key="key", exit_on_error=False)


def test_upload_network_error(sample_json_file):
    with patch("requests.post", side_effect=requests.ConnectionError("Connection timed out")):
        with pytest.raises(requests.RequestException):
            upload_catalog(sample_json_file, api_key="key", exit_on_error=False)


def test_get_default_master_file(tmp_path):
    from src.uploader import get_default_master_file

    with patch("src.uploader.config") as mock_cfg:
        mock_cfg.output_jsonl_path = tmp_path / "master.jsonl"
        mock_cfg.data_outputs_dir = tmp_path
        
        # When jsonl doesn't exist but json exists
        json_fallback = tmp_path / "university_counseling_data.json"
        json_fallback.write_text("[]", encoding="utf-8")
        assert get_default_master_file() == json_fallback

        # When jsonl exists, it takes precedence
        mock_cfg.output_jsonl_path.write_text("{}\n", encoding="utf-8")
        assert get_default_master_file() == mock_cfg.output_jsonl_path


def test_upload_catalog_defaults_to_master(sample_jsonl_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"summary": {"universities_created": 2}}

    with patch("src.uploader.get_default_master_file", return_value=sample_jsonl_file):
        with patch("requests.post", return_value=mock_resp) as mock_post:
            res = upload_catalog(file_path=None, api_key="secret", exit_on_error=False)
            assert res["summary"]["universities_created"] == 2
            args, kwargs = mock_post.call_args
            assert len(kwargs["json"]) == 2


