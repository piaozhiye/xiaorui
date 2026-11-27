import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools/provision-music-pairing.sh"


def test_local_pairing_provisioner_generates_valid_ca_and_board_certificate(tmp_path):
    ca_dir = tmp_path / "ca"
    output_dir = tmp_path / "output"
    subprocess.run([
        str(SCRIPT), "--generate-only", "--hostname", "xiaorui-music.test",
        "--ip", "192.0.2.40", "--ca-dir", str(ca_dir), "--output-dir", str(output_dir),
    ], check=True, capture_output=True, text=True)

    root_ca = output_dir / "xiaorui-music-root-ca.crt"
    leaf_cert = output_dir / "music-ui.crt"
    leaf_key = output_dir / "music-ui.key"
    assert root_ca.is_file() and leaf_cert.is_file() and leaf_key.is_file()
    assert (ca_dir / "root-ca.key").stat().st_mode & 0o777 == 0o600
    assert leaf_key.stat().st_mode & 0o777 == 0o600
    assert subprocess.run([
        "openssl", "verify", "-CAfile", str(root_ca), str(leaf_cert),
    ], check=False, capture_output=True, text=True).returncode == 0
    for name in ("xiaorui-music.test", "192.0.2.40"):
        subprocess.run([
            "openssl", "x509", "-in", str(leaf_cert), "-noout",
            "-checkhost" if not name[0].isdigit() else "-checkip", name,
        ], check=True, capture_output=True, text=True)
