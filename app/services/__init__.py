"""Business-logic layer shared by API routers and the batch job.

Intentionally empty. Routers should call into services, not the other
way around, and the batch entrypoint (app.batch.run_daily) should share
this layer with the API rather than duplicating logic — but no service
module exists yet because no business logic has been built. Later
agents (P1-FETCH, P3-LLM, ADMIN-API, AUTH-OTP, ...) add modules here as
their features land.
"""
