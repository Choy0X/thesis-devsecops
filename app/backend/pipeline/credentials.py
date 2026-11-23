"""What looks like credentials, in one place: the names of files and folders that are kept from anything that reads a project on a model's behalf.

The project chat's tools and the project index (the vectors the chat searches) both read the person's code, and what a model is shown must never include a credential store. Two
rules that drifted apart would be a hole in one of them, so both ask :func:`is_kept`. A path is kept when any folder in it is a credential folder (``.git``, ``.ssh``, ``.aws``...) or
its file name looks like one (``.env``, keys, tokens, state files). Names are compared as the file system sees them: Windows drops trailing dots and spaces and treats
``name:stream`` as the file ``name``, so ``.env.`` and ``.env::$DATA`` are ``.env``.
"""
from __future__ import annotations

import re

SECRET_FILE = re.compile(
    r"^\.env(\..*)?$|^\.envrc$|\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk|p8|asc|gpg|pgp|tfvars|tfstate|secret|vault)$|^id_(rsa|dsa|ecdsa|ed25519)|"
    r"^\.(npmrc|pypirc|netrc|git-credentials|pgpass|my\.cnf|htpasswd|dockercfg|vault[_-]pass)$|^credentials?(\..*)?$|^secrets?\.(json|ya?ml|toml|txt|env|ini|conf|properties)$|"
    r"^service[-_]?account.*\.json$|^terraform\.tfstate|^kubeconfig$|^(auth|tokens?)\.(json|txt|ya?ml)$", re.I)
SECRET_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".gcloud", ".config", ".secrets", "secrets", ".vault"}


def plain(name: str) -> str:
    """A name as the file system sees it (see the module's note)."""
    return name.split(":", 1)[0].rstrip(" .")


def is_secret_dir(name: str) -> bool:
    return plain(name).lower() in SECRET_DIRS


def is_kept(rel: str) -> bool:
    """Whether a path (relative, any separator) is one that is kept from a model: a folder of credentials in it, or a file named like credentials."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    if any(is_secret_dir(p) for p in parts):
        return True
    return bool(SECRET_FILE.search(plain(parts[-1])))
