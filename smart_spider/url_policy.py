# coding=utf-8
"""Outbound URL validation and SSRF guardrails.

The policy is intentionally small and dependency-free.  It rejects unsafe
schemes, embedded credentials, localhost names and literal/resolved private
addresses before a request is issued.  Applications that intentionally crawl
an internal network can opt in with ``allow_private_hosts=True``.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL violates the outbound request policy."""


@dataclass(frozen=True)
class URLPolicy:
    allow_private_hosts: bool = False
    resolve_dns: bool = True
    allowed_schemes: tuple[str, ...] = ("http", "https")
    max_url_length: int = 8192

    def _parse(self, url: str):
        if not isinstance(url, str) or not url.strip():
            raise UnsafeURLError("URL must be a non-empty string")
        url = url.strip()
        if len(url) > self.max_url_length:
            raise UnsafeURLError("URL exceeds maximum length")
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in self.allowed_schemes:
            raise UnsafeURLError(f"URL scheme is not allowed: {parsed.scheme or '<missing>'}")
        if parsed.username or parsed.password:
            raise UnsafeURLError("URL credentials are not allowed")
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            raise UnsafeURLError("URL host is missing")
        return url, parsed, host

    @staticmethod
    def _is_unsafe_address(address: str) -> bool:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError:
            return True
        return (
            parsed_address.is_private
            or parsed_address.is_loopback
            or parsed_address.is_link_local
            or parsed_address.is_multicast
            or parsed_address.is_unspecified
            or parsed_address.is_reserved
            or not parsed_address.is_global
        )

    def resolve_public_addresses(self, url: str) -> frozenset[str]:
        """Resolve and validate the target used for one outbound connection.

        The HTTP client calls this directly before each hop, then compares any
        exposed socket peer address after connecting.  That closes the usual
        DNS-rebinding window without replacing the caller's TLS/SNI handling.
        """
        _, parsed, host = self._parse(url)
        scheme = parsed.scheme.lower()
        if self.allow_private_hosts:
            return frozenset()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise UnsafeURLError(f"private host is not allowed: {host}")
        addresses: set[str] = set()
        try:
            literal = ipaddress.ip_address(host)
            addresses.add(str(literal))
        except ValueError:
            if not self.resolve_dns:
                return frozenset()
            try:
                addresses.update(
                    str(item[4][0])
                    for item in socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
                )
            except (OSError, ValueError):
                # Let the HTTP layer report normal DNS failures.  This avoids
                # turning transient resolver errors into policy false positives.
                return frozenset()
        for address in addresses:
            if self._is_unsafe_address(address):
                raise UnsafeURLError(f"private or reserved address is not allowed: {address}")
        return frozenset(addresses)

    def validate_peer(self, peer_address: str, resolved_addresses: frozenset[str]) -> None:
        """Reject a private or changed peer address after a DNS validation."""
        if self.allow_private_hosts:
            return
        if self._is_unsafe_address(peer_address):
            raise UnsafeURLError(
                f"private or reserved connected peer is not allowed: {peer_address}"
            )
        if resolved_addresses and peer_address not in resolved_addresses:
            raise UnsafeURLError(
                "connected peer is not one of the addresses validated for this URL"
            )

    def validate(self, url: str) -> str:
        """Validate URL syntax and the currently resolved target addresses."""
        url, _, _ = self._parse(url)
        self.resolve_public_addresses(url)
        return url
