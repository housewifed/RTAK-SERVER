"""Build ATAK/iTAK enrollment data packages (MissionPackage zips).

An enrollment data package is the turnkey way to onboard a TAK client: the
user imports one file (or scans a ``tak://...import`` QR that downloads it)
and ATAK is fully configured — server address, trusted CA, and
auto-enrollment all set. The user only has to enter their token when
prompted. This removes the fragile "copy the right truststore.p12 by hand"
step that otherwise causes ``unknown ca`` failures.

Format reference: ATAK Certificate Auto-Enrollment data package.
  MANIFEST/MANIFEST.xml
  certs/config.pref          (points caLocation at cert/caCert.p12)
  certs/caCert.p12           (the trusted CA — our truststore)

Note the deliberate ``certs/`` (zip folder) vs ``cert/`` (runtime path)
distinction: ATAK copies certs from the package's certs/ folder into its
internal cert/ directory, which is what config.pref references.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Optional
from xml.sax.saxutils import escape

CONFIG_PREF = """<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
  <preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{description}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{host}:{port}:ssl</entry>
    <entry key="caLocation0" class="class java.lang.String">cert/caCert.p12</entry>
    <entry key="caPassword0" class="class java.lang.String">{ca_password}</entry>
    <entry key="enrollForCertificateWithTrust0" class="class java.lang.Boolean">true</entry>
    <entry key="useAuth0" class="class java.lang.Boolean">true</entry>
    <entry key="cacheCreds0" class="class java.lang.String">Cache credentials</entry>
  </preference>
  <preference version="1" name="com.atakmap.app_preferences">
    <entry key="displayServerConnectionWidget" class="class java.lang.Boolean">true</entry>
{callsign_entry}  </preference>
</preferences>
"""

MANIFEST = """<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveDelete" value="true"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="certs/config.pref"/>
    <Content ignore="false" zipEntry="certs/caCert.p12"/>
  </Contents>
</MissionPackageManifest>
"""


SOFT_CONFIG_PREF = """<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
  <preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{description}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{host}:{port}:ssl</entry>
    <entry key="caLocation0" class="class java.lang.String">cert/caCert.p12</entry>
    <entry key="caPassword0" class="class java.lang.String">{ca_password}</entry>
    <entry key="certificateLocation0" class="class java.lang.String">cert/clientCert.p12</entry>
    <entry key="clientPassword0" class="class java.lang.String">{client_password}</entry>
  </preference>
  <preference version="1" name="com.atakmap.app_preferences">
    <entry key="displayServerConnectionWidget" class="class java.lang.Boolean">true</entry>
{callsign_entry}  </preference>
</preferences>
"""

SOFT_MANIFEST = """<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveDelete" value="true"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="certs/config.pref"/>
    <Content ignore="false" zipEntry="certs/caCert.p12"/>
    <Content ignore="false" zipEntry="certs/clientCert.p12"/>
  </Contents>
</MissionPackageManifest>
"""


def build_softcert_package(host: str, ca_p12: bytes, client_p12: bytes,
                           description: str = "TAK-Revamp",
                           port: int = 8089,
                           callsign: Optional[str] = None,
                           ca_password: str = "atakatak",
                           client_password: str = "atakatak") -> bytes:
    """Return an ATAK data package containing a ready-to-use client identity.

    Unlike the auto-enrollment package, this needs no token and no on-device
    enrollment: the device imports a signed client cert and connects with it
    immediately (mutual TLS). One scan, zero typing.
    """
    callsign_entry = ""
    if callsign:
        callsign_entry = (
            '    <entry key="locationCallsign" class="class java.lang.String">'
            f"{escape(callsign)}</entry>\n"
        )
    config = SOFT_CONFIG_PREF.format(
        description=escape(description), host=escape(host), port=port,
        ca_password=escape(ca_password), client_password=escape(client_password),
        callsign_entry=callsign_entry,
    )
    manifest = SOFT_MANIFEST.format(
        uid=str(uuid.uuid4()), name=f"{description}_Connect.zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST/MANIFEST.xml", manifest)
        z.writestr("certs/config.pref", config)
        z.writestr("certs/caCert.p12", ca_p12)
        z.writestr("certs/clientCert.p12", client_p12)
    return buf.getvalue()


def build_enrollment_package(host: str, ca_p12: bytes,
                             description: str = "TAK-Revamp",
                             port: int = 8089,
                             callsign: Optional[str] = None,
                             ca_password: str = "atakatak") -> bytes:
    """Return the bytes of an ATAK auto-enrollment data package zip."""
    callsign_entry = ""
    if callsign:
        callsign_entry = (
            '    <entry key="locationCallsign" class="class java.lang.String">'
            f"{escape(callsign)}</entry>\n"
        )
    config = CONFIG_PREF.format(
        description=escape(description), host=escape(host), port=port,
        ca_password=escape(ca_password), callsign_entry=callsign_entry,
    )
    manifest = MANIFEST.format(
        uid=str(uuid.uuid4()),
        name=f"{description}_Enrollment.zip",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST/MANIFEST.xml", manifest)
        z.writestr("certs/config.pref", config)
        z.writestr("certs/caCert.p12", ca_p12)
    return buf.getvalue()
