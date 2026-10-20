"""Bounded JSON/bytes local-process boundary; never live resource/exception graphs.

multiprocessing transports these byte strings only between owned local children.
This is NOT a network protocol and accepts no arbitrary pickle input.
"""
import json
from uuid import UUID

from drt_scheduler import JobDefinition, TaskDefinition, ResultRejectedError

VERSION = 1
MAX_INIT_BYTES = 32768
MAX_TASK_BYTES = 4096
MAX_RESULT_BYTES = 16384
INT64 = (1 << 63) - 1
JOB_KEYS = {'run_id', 'workload_id', 'workload_version', 'resource_identity', 'description',
            'expected_units', 'configuration_identity', 'job_identity'}
TASK_KEYS = {'run_id', 'job_identity', 'configuration_identity', 'resource_identity',
             'task_id', 'payload_identity', 'ordinal', 'expected_units', 'payload'}
HEADER_KEYS = {'version', 'kind', 'worker_id', 'pid', 'run_id', 'job_identity',
               'configuration_identity', 'resource_identity'}


class LocalProtocolError(ResultRejectedError):
    pass


class WorkerFailure(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')


def require(value, message):
    if not value:
        raise LocalProtocolError(message)


def integer(value, minimum=0, maximum=INT64):
    require(type(value) is int and minimum <= value <= maximum, 'Invalid bounded integer.')
    return value


def encode(value, maximum=MAX_RESULT_BYTES):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
    require(len(raw) <= maximum, 'Local packet exceeds byte bound.')
    return raw


def decode(raw, maximum=MAX_RESULT_BYTES):
    require(type(raw) is bytes and 0 < len(raw) <= maximum, 'Invalid local packet bytes/size.')

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate local JSON field.')
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
                           parse_constant=lambda _: require(False, 'Nonfinite JSON.'))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise LocalProtocolError('Malformed local JSON.') from error
    require(type(value) is dict and type(value.get('version')) is int
            and value['version'] == VERSION, 'Incompatible local packet.')
    return value


def job_data(job):
    return dict(run_id=str(job.run_id), workload_id=job.workload_id, workload_version=job.workload_version,
                resource_identity=job.resource_identity, description=job.description.decode('ascii'),
                expected_units=job.expected_units, configuration_identity=job.configuration_identity,
                job_identity=job.job_identity)


def read_job(data):
    require(type(data) is dict and set(data) == JOB_KEYS, 'Invalid job fields.')
    try:
        job = JobDefinition(UUID(data['run_id']), data['workload_id'], data['workload_version'],
                            data['resource_identity'], data['description'].encode('ascii'), data['expected_units'])
    except (ValueError, TypeError, AttributeError, UnicodeError) as error:
        raise LocalProtocolError('Invalid job definition.') from error
    require(job_data(job) == data, 'Job identity/canonical description differs.')
    return job








def task_data(task):
    job = task.job
    return dict(run_id=str(job.run_id), job_identity=job.job_identity,
                configuration_identity=job.configuration_identity, resource_identity=job.resource_identity,
                task_id=task.task_id, payload_identity=task.payload_identity, ordinal=task.ordinal,
                expected_units=task.expected_units, payload=task.payload.decode('ascii'))


def task_packet(task):
    return encode(dict(version=VERSION, kind='task', task=task_data(task)), MAX_TASK_BYTES)


def read_task(message, job):
    require(set(message) == {'version', 'kind', 'task'} and message['kind'] == 'task', 'Invalid task packet.')
    data = message['task']
    require(type(data) is dict and set(data) == TASK_KEYS, 'Invalid task fields.')
    task = TaskDefinition(job, integer(data['ordinal']), integer(data['expected_units'], 1, 256),
                          data['payload'].encode('ascii'))
    require(task_data(task) == data, 'Foreign or modified task identity.')
    return task


def header(kind, job, worker_id, pid):
    return dict(version=VERSION, kind=kind, worker_id=worker_id, pid=pid,
                run_id=str(job.run_id), job_identity=job.job_identity,
                configuration_identity=job.configuration_identity, resource_identity=job.resource_identity)


def check_header(message, job, worker_id, pid):
    require(all(message.get(k) == v and type(message.get(k)) is type(v)
                for k, v in header(message.get('kind'), job, worker_id, pid).items()),
            'Worker PID/run/job/configuration/resource correlation differs.')


def failure_data(error):
    return dict(code=str(getattr(error, 'code', type(error).__name__))[:128],
                message=(str(error) or type(error).__name__)[:2048])


def read_failure(value):
    require(type(value) is dict and set(value) == {'code', 'message'}
            and type(value['code']) is str and 0 < len(value['code']) <= 128
            and type(value['message']) is str and len(value['message']) <= 2048, 'Invalid typed failure.')
    if value['code'] == 'ShadowNumericalError':
        from drt_backend.raytracing.tracing.surface_queries import ShadowNumericalError
        return ShadowNumericalError(value['message'])
    return WorkerFailure(value['code'], value['message'])






