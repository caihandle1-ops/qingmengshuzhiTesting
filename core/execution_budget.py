"""Request-local cooperative deadlines; no worker thread or forced cancellation.

Requests connect/read timeouts are inactivity limits, not a hard wall-clock
timer. This budget refuses later stages after expiry, narrows future network
timeouts and records overruns of already blocking work truthfully.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
from time import perf_counter

_current = ContextVar('financial_execution_budget', default=None)


class BudgetExceeded(TimeoutError):
    pass


class ExecutionBudget:
    def __init__(self, seconds=60, *, clock=None):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('执行预算必须是正的有限秒数。')
        self.seconds = float(seconds)
        self.clock = clock or perf_counter
        self.started = self.clock()
        self.deadline = self.started + self.seconds
        self.events = []
        self.network_timeouts = []

    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def check(self, stage='下一阶段'):
        remaining = self.remaining()
        self.events.append({'stage': stage, 'elapsed_ms': round((self.clock() - self.started) * 1000, 3),
                            'remaining_ms': round(remaining * 1000, 3), 'status': 'available' if remaining > 0 else 'expired'})
        if remaining <= 0:
            raise BudgetExceeded(f'{stage}开始前，{self.seconds:g}秒协作预算已用尽；不启动新的未验证结果路径。')

    def network_timeout(self, connect_cap=5, read_cap=45):
        self.check('云端网络请求')
        remaining = self.remaining()
        if remaining <= 0.002:
            raise BudgetExceeded('剩余协作预算不足以启动网络请求。')
        connect = min(float(connect_cap), remaining * 0.2)
        read = min(float(read_cap), remaining - connect)
        if connect <= 0 or read <= 0:
            raise ValueError('网络连接和读取上限必须为正数。')
        timeout = (connect, read)
        self.network_timeouts.append({'connect_seconds': connect, 'read_seconds': read,
                                      'remaining_seconds_at_dispatch': remaining})
        return timeout

    def snapshot(self):
        elapsed = max(0.0, self.clock() - self.started)
        return {'budget_seconds': self.seconds, 'elapsed_ms': round(elapsed * 1000, 3),
                'remaining_ms': round(max(0.0, self.seconds - elapsed) * 1000, 3),
                'exceeded': elapsed >= self.seconds, 'enforcement': 'cooperative',
                'events': list(self.events), 'network_timeouts': list(self.network_timeouts),
                'limitations': '阶段边界协作预算；网络超时为连接/读取等待限制。不能强制中断已阻塞的调用，也不保证进程恰在60秒结束。'}


def current_budget():
    return _current.get()


def check_budget(stage):
    budget = current_budget()
    if budget is not None:
        budget.check(stage)


@contextmanager
def execution_budget(seconds=60, *, budget=None):
    existing = current_budget()
    if existing is not None:
        yield existing
        return
    active = budget or ExecutionBudget(seconds)
    token = _current.set(active)
    try:
        yield active
    finally:
        _current.reset(token)


def budgeted_answer(function):
    """Give every return path budget metadata; preserve unexpected exceptions."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with execution_budget(kwargs.get('budget_seconds', 60)) as budget:
            call_started = budget.clock()
            try:
                budget.check('问答开始')
                result = function(*args, **kwargs)
            except BudgetExceeded as error:
                result = {'status': 'budget_exceeded', 'reason': str(error), 'answer':
                          '本次问答协作预算已用尽。尚未完成的事实核验没有被标为通过，请缩小问题范围后重试。',
                          'draft': '', 'parsed': {}, 'evidence': '', 'chart': None, 'report_text': None,
                          'model_used': '可信数据分析', 'company': '', 'agent_trace': [],
                          'sql_status': 'not_completed', 'sql_result': {'status': 'not_completed', 'rows': [], 'attempts': []},
                          'query_plan': {}, 'retrieval_result': {'status': 'not_completed', 'citations': [], 'hits': []},
                          'analysis_intent': 'budget_exceeded', 'attribution': None, 'missing_scope': []}
            result['execution_budget'] = budget.snapshot()
            result['duration_ms'] = round(max(0.0, budget.clock() - call_started) * 1000, 3)
            return result
    return wrapped
