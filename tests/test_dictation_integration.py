import asyncio
import unittest
from unittest.mock import AsyncMock

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from src.dictation.web import DictationService


class DictationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = DictationService()
        app = web.Application(client_max_size=12 * 1024 * 1024)
        self.service.setup_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def test_health_does_not_eagerly_start_worker(self):
        response = await self.client.get("/api/dictation/health")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["workerRunning"])

    async def test_catalog_contract_matches_5003_frontend(self):
        root = await (await self.client.get("/api/catalog")).json()
        self.assertEqual(len(root["subjects"]), 2)
        volumes = await (await self.client.get("/api/catalog/chinese/volumes")).json()
        self.assertEqual(len(volumes["volumes"]), 12)
        volume_id = volumes["volumes"][0]["id"]
        units = await (await self.client.get(
            f"/api/catalog/chinese/volumes/{volume_id}/units"
        )).json()
        self.assertIn("units", units)

    async def test_dictionary_and_parent_routes_are_same_origin(self):
        response = await self.client.post("/api/dictionary/examples", json={"chars": ["好"]})
        self.assertEqual(response.status, 200)
        self.assertIn("好", (await response.json())["examples"])
        parent = await self.client.get("/parent")
        self.assertEqual(parent.status, 200)

    async def test_worker_starts_on_demand_without_loading_ocr(self):
        payload = await self.service.worker.request("ping", timeout=10)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["ocrLoaded"])
        self.assertEqual(payload["stdinEncoding"].lower().replace("-", ""), "utf8")
        self.assertEqual(payload["stdoutEncoding"].lower().replace("-", ""), "utf8")

    async def test_saved_history_asset_is_not_uploaded_again_for_ocr(self):
        history = await (await self.client.get("/api/history")).json()
        self.assertTrue(history["items"])
        self.service.worker.request = AsyncMock(return_value={"items": [], "warnings": []})
        form = FormData()
        form.add_field("assetId", history["items"][0]["name"])
        form.add_field("mode", "zh")
        form.add_field("crop", '{"x":0,"y":0,"w":1,"h":1}')
        response = await self.client.post("/api/ocr/jobs", data=form)
        self.assertEqual(response.status, 202)
        job_id = (await response.json())["jobId"]
        for _ in range(20):
            await asyncio.sleep(0.01)
            job = await (await self.client.get(f"/api/ocr/jobs/{job_id}")).json()
            if job["status"] == "completed":
                break
        self.assertEqual(job["status"], "completed")


if __name__ == "__main__":
    unittest.main()
