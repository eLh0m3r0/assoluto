"""Background-task bodies.

These are plain functions (sync or async) invoked from FastAPI
`BackgroundTasks` or from APScheduler jobs. Keeping them in a single
package means we can later migrate to a real queue (Dramatiq+Redis) by
swapping the invocation site without touching the business logic.
"""
