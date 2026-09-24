"""Pydantic schemas for the inventory (IPAM/DCIM) API."""

import ipaddress
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORMModel(BaseModel):
    """Base schema with ORM mode enabled."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- Site
class SiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    slug: str = Field(min_length=1, max_length=150, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    status: str = Field(default="active", max_length=30)
    facility: str | None = None
    address: str | None = None
    description: str | None = None


class SiteCreate(SiteBase):
    pass


class SiteUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    status: str | None = None
    facility: str | None = None
    address: str | None = None
    description: str | None = None


class SiteRead(SiteBase, ORMModel):
    id: int
    created_at: datetime
    updated_at: datetime

    rack_count: int = 0
    device_count: int = 0


# ---------------------------------------------------------------- Rack
class RackBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    site_id: int
    status: str = Field(default="active", max_length=30)
    u_height: int = Field(default=42, ge=1, le=120)
    position: int | None = None
    role: str | None = None
    description: str | None = None


class RackCreate(RackBase):
    pass


class RackUpdate(BaseModel):
    name: str | None = None
    site_id: int | None = None
    status: str | None = None
    u_height: int | None = None
    position: int | None = None
    role: str | None = None
    description: str | None = None


class RackRead(RackBase, ORMModel):
    id: int
    created_at: datetime
    updated_at: datetime

    site_name: str = ""
    device_count: int = 0


# ---------------------------------------------------------------- Device
class DeviceBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    device_type: str = Field(min_length=1, max_length=150)
    role: str | None = None
    status: str = Field(default="active", max_length=30)
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    asset_tag: str | None = None
    site_id: int
    rack_id: int | None = None
    rack_position: int | None = None
    description: str | None = None


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    name: str | None = None
    device_type: str | None = None
    role: str | None = None
    status: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    asset_tag: str | None = None
    site_id: int | None = None
    rack_id: int | None = None
    rack_position: int | None = None
    description: str | None = None


class DeviceRead(DeviceBase, ORMModel):
    id: int
    created_at: datetime
    updated_at: datetime

    site_name: str = ""
    rack_name: str | None = None
    ip_count: int = 0


# ---------------------------------------------------------------- IP Address
def _validate_address(value: str | None) -> str | None:
    """Require a valid IPv4/IPv6 address with an optional CIDR prefix."""
    if value is None:
        return None
    try:
        ipaddress.ip_interface(value)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {value!r}") from exc
    return value


class IPAddressBase(BaseModel):
    address: str = Field(min_length=3, max_length=64)
    status: str = Field(default="active", max_length=30)
    dns_name: str | None = None
    role: str | None = None
    description: str | None = None
    device_id: int | None = None
    interface: str | None = None

    _ipcheck = field_validator("address")(_validate_address)


class IPAddressCreate(IPAddressBase):
    pass


class IPAddressUpdate(BaseModel):
    address: str | None = None
    status: str | None = None
    dns_name: str | None = None
    role: str | None = None
    description: str | None = None
    device_id: int | None = None
    interface: str | None = None

    _ipcheck = field_validator("address")(_validate_address)


class IPAddressRead(IPAddressBase, ORMModel):
    id: int
    created_at: datetime
    updated_at: datetime

    device_name: str | None = None


# ---------------------------------------------------------------- Search
class SearchResult(BaseModel):
    devices: list[DeviceRead] = []
    ip_addresses: list[IPAddressRead] = []
    sites: list[SiteRead] = []
    racks: list[RackRead] = []


# ---------------------------------------------------------------- Overview
class InventoryOverview(BaseModel):
    sites: int = 0
    racks: int = 0
    devices: int = 0
    ip_addresses: int = 0
    active_devices: int = 0
