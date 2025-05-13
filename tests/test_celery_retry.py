import pytest
from celery import Celery
from celery.exceptions import Retry

# Create a local Celery app for testing, with eager mode and exception propagation
app = Celery("test_app", broker="memory://", backend="rpc://")
app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
)

# Bind our flaky task to this app
@app.task(bind=True, max_retries=2, default_retry_delay=1)
def flaky(self, fail_first=[True]):
    if fail_first[0]:
        fail_first[0] = False
        # This will be raised and propagated thanks to task_eager_propagates=True
        raise self.retry(exc=Exception("transient error"))
    return "success"

def test_flaky_retries_propagates_retry():
    # 1) First call should immediately raise Retry
    with pytest.raises(Retry):
        flaky.apply().get()
    # 2) Second call should now succeed
    result = flaky.apply().get()
    assert result == "success"
