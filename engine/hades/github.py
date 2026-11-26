"""Minimal GitHub REST client (public repos). All calls go through Http (rate limit + retry)."""

from typing import Any

from hades.http import Http, SourceError

API = "https://api.github.com"


class GitHub:
    def __init__(self, http: Http, token: str = "") -> None:
        self.http = http
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, endpoint: str, **params: Any) -> Any:
        return await self.http.get_json(f"{API}{endpoint}", params=params or None, headers=self.headers)

    async def commits(self, repo: str, path: str, per_page: int = 30) -> list[dict]:
        return await self._get(f"/repos/{repo}/commits", path=path, per_page=per_page)

    async def commit(self, repo: str, sha: str) -> dict:
        return await self._get(f"/repos/{repo}/commits/{sha}")

    async def compare(self, repo: str, base: str, head: str) -> dict:
        return await self._get(f"/repos/{repo}/compare/{base}...{head}")

    async def file(self, repo: str, path: str, ref: str) -> str | None:
        """Raw file contents at `ref`, or None if it does not exist there."""
        try:
            return await self.http.get_text(f"{API}/repos/{repo}/contents/{path}", params={"ref": ref},
                                            headers={**self.headers, "Accept": "application/vnd.github.raw"})
        except SourceError as e:
            if e.status == 404:
                return None
            raise

    async def open_pulls(self, repo: str, per_page: int = 20) -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls", state="open", sort="updated", direction="desc",
                               per_page=per_page)

    async def pull_files(self, repo: str, number: int) -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls/{number}/files", per_page=100)

    async def commit_pulls(self, repo: str, sha: str) -> list[dict]:
        return await self._get(f"/repos/{repo}/commits/{sha}/pulls")
