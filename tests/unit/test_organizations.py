"""Unit tests for organization endpoints."""


import pytest

pytestmark = pytest.mark.no_mock_auth

API_BASE = "http://localhost:8000/api/v1"


def create_organization(client, url, **kwargs):
    response = client.post(url, **kwargs)
    if response.status_code == 201:
        org_id = response.json()["id"]
        signup = client.post(
            f"{API_BASE}/auth/signup",
            json={
                "org_id": org_id,
                "name": "Owner",
                "email": f"{org_id}@example.com",
                "password": "TestPass123!",
            },
        )
        assert signup.status_code == 201, signup.text
        client.headers["Authorization"] = f"Bearer {signup.json()['token']}"
    return response


class TestOrganizationCreate:
    """Test organization creation."""

    def test_create_org_success(self, client):
        """Test successful organization creation."""
        response = create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={
                "id": "test_org_001_v2",
                "name": "Test Organization",
                "region": "Test Region",
                "config": {"location": "Test City"},
            },
        )
        assert response.status_code in [200, 201]
        data = response.json()
        assert data["id"] == "test_org_001_v2"
        assert data["name"] == "Test Organization"

    def test_create_org_duplicate_id(self, client):
        """Test creating org with duplicate ID fails."""
        # Create first org
        create_organization(
            client, f"{API_BASE}/organizations/", json={"id": "test_org_002", "name": "First Org"}
        )
        # Try to create duplicate
        response = create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={"id": "test_org_002", "name": "Duplicate Org"},
        )
        assert response.status_code == 409  # Conflict

    def test_create_org_missing_name(self, client):
        """Test creating org without name fails."""
        response = create_organization(
            client, f"{API_BASE}/organizations/", json={"id": "test_org_003"}
        )
        assert response.status_code == 422  # Validation error

    def test_create_org_empty_id(self, client):
        """Test creating org with empty ID fails."""
        response = create_organization(
            client, f"{API_BASE}/organizations/", json={"id": "", "name": "Empty ID Org"}
        )
        assert response.status_code == 422


class TestOrganizationRead:
    """Test organization retrieval."""

    def test_get_org_success(self, client):
        """Test successful organization retrieval."""
        # Create org first
        create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={"id": "test_org_004", "name": "Get Test Org"},
        )
        # Retrieve it
        response = client.get(f"{API_BASE}/organizations/test_org_004")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test_org_004"
        assert data["name"] == "Get Test Org"

    def test_get_org_requires_membership(self, client):
        """Test retrieving non-existent org requires membership."""
        response = client.get(f"{API_BASE}/organizations/nonexistent_org")
        assert response.status_code == 403

    def test_list_orgs(self, client):
        """Test listing all organizations."""
        # Create a few orgs
        for i in range(5, 8):
            create_organization(
                client,
                f"{API_BASE}/organizations/",
                json={"id": f"test_org_{i:03d}", "name": f"List Test Org {i}"},
            )
        # List them
        response = client.get(f"{API_BASE}/organizations/")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "test_org_007"


class TestOrganizationUpdate:
    """Test organization updates."""

    def test_update_org_success(self, client):
        """Test successful organization update."""
        # Create org
        create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={"id": "test_org_008_v2", "name": "Original Name"},
        )
        # Update it
        response = client.put(
            f"{API_BASE}/organizations/test_org_008_v2",
            json={"name": "Updated Name", "region": "New Region"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data.get("region") == "New Region"

    def test_update_org_requires_membership(self, client):
        """Test updating non-existent org requires membership."""
        response = client.put(
            f"{API_BASE}/organizations/nonexistent_org", json={"name": "Updated Name"}
        )
        assert response.status_code == 403

    def test_update_org_partial(self, client):
        """Test partial update of organization."""
        # Create org
        create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={"id": "test_org_009_v2", "name": "Original", "region": "Original Region"},
        )
        # Update only name
        response = client.put(
            f"{API_BASE}/organizations/test_org_009_v2", json={"name": "New Name"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data.get("region") == "Original Region"


class TestOrganizationDelete:
    """Test organization deletion."""

    def test_delete_org_success(self, client):
        """Test successful organization deletion."""
        # Create org
        create_organization(
            client,
            f"{API_BASE}/organizations/",
            json={"id": "test_org_010", "name": "To Be Deleted"},
        )
        # Delete it
        response = client.delete(f"{API_BASE}/organizations/test_org_010")
        assert response.status_code in [200, 204]  # OK or No Content
        # Verify it's gone
        response = client.get(f"{API_BASE}/organizations/test_org_010")
        assert response.status_code == 401

    def test_delete_org_requires_membership(self, client):
        """Test deleting non-existent org requires membership."""
        response = client.delete(f"{API_BASE}/organizations/nonexistent_org")
        assert response.status_code == 403
