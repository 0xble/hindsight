"""Transparent runtime fixture generation, with no retained binary PDFs."""

import hashlib
import struct

from pdfminer.arcfour import Arcfour
from pdfminer.pdfdocument import PDFStandardSecurityHandler

SYNTHETIC_PASSWORD = "synthetic-fixture-password"


def encrypted_pdf() -> bytes:
    """Build a minimal valid PDF using the existing parser's revision-2 cipher.

    This legacy RC4 handler is solely a test of password rejection, not a
    security recommendation. All contents and passwords are synthetic. The
    owner/user entries and encrypted stream are computed transparently from
    PDF standard algorithms 3.2, 3.3, 3.4 and the per-object key derivation,
    matching pdfminer's existing PDFStandardSecurityHandler implementation.
    """
    padding = PDFStandardSecurityHandler.PASSWORD_PADDING
    user_password = (SYNTHETIC_PASSWORD.encode() + padding)[:32]
    owner_password = (b"synthetic-owner-password" + padding)[:32]
    owner_key = hashlib.md5(owner_password).digest()[:5]
    owner_entry = Arcfour(owner_key).encrypt(user_password)
    document_id = hashlib.md5(b"transparent-encrypted-pdf-fixture").digest()
    permissions = struct.pack("<i", -4)
    encryption_key = hashlib.md5(user_password + owner_entry + permissions + document_id).digest()[:5]
    user_entry = Arcfour(encryption_key).encrypt(padding)
    object_key = hashlib.md5(encryption_key + struct.pack("<I", 5)[:3] + struct.pack("<I", 0)[:2]).digest()[:10]
    stream = Arcfour(object_key).encrypt(b"BT /F1 12 Tf 15 80 Td (Alice completed the review.) Tj ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        (
            b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <"
            + owner_entry.hex().encode()
            + b"> /U <"
            + user_entry.hex().encode()
            + b"> >>"
        ),
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        b"trailer\n<< /Size 7 /Root 1 0 R /Encrypt 6 0 R /ID [<"
        + document_id.hex().encode()
        + b"> <"
        + document_id.hex().encode()
        + b">] >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )
    return bytes(output)
