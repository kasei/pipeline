from threading import Lock
from contextlib import ContextDecorator, suppress
from collections import defaultdict

import settings
import pipeline.io.arches
from cromulent.model import factory

def identity(d):
	'''
	Simply yield the value that is passed as an argument.
	
	This is trivial, but necessary for use in constructing some bonobo graphs.
	For example, if two already instantiated graph chains need to be connected,
	one being used as input to the other, bonobo does not allow this:
	
	`graph.add_chain(_input=prefix.output, _output=suffix.input)`
	
	Instead, the `add_chain` call requires at least one graph node to be added. Hence:

	`graph.add_chain(identity, _input=prefix.output, _output=suffix.input)`
	'''
	yield d

class ExclusiveValue(ContextDecorator):
	_locks = {}
	lock = Lock()

	def __init__(self, wrapped):
		self._wrapped = wrapped

	def get_lock(self):
		_id = self._wrapped
		with ExclusiveValue.lock:
			if not _id in ExclusiveValue._locks:
				ExclusiveValue._locks[_id] = Lock()
		return ExclusiveValue._locks[_id]

	def __enter__(self):
		self.get_lock().acquire()
		return self._wrapped

	def __exit__(self, *exc):
		self.get_lock().release()

def configured_arches_writer():
	return pipeline.io.arches.ArchesWriter(
		endpoint=settings.arches_endpoint,
		auth_endpoint=settings.arches_auth_endpoint,
		username=settings.arches_endpoint_username,
		password=settings.arches_endpoint_password,
		client_id=settings.arches_client_id
	)

class CromObjectMerger:
	def merge(self, obj, *to_merge):
		print('merging...')
		propInfo = obj._list_all_props()
# 		print(f'base object: {obj}')
		for m in to_merge:
			pass
# 			print('============================================')
# 			print(f'merge: {m}')
			for p in propInfo.keys():
				value = None
				with suppress(AttributeError):
					value = getattr(m, p)
				if value is not None:
# 					print(f'{p}: {value}')
					if type(value) == list:
						self.set_or_merge(obj, p, *value)
					else:
						self.set_or_merge(obj, p, value)
# 			obj = self.merge(obj, m)
		print('Result of merge:')
		print(factory.toString(obj, False))
		return obj

	def set_or_merge(self, obj, p, *values):
		print('------------------------')
		existing = []
		with suppress(AttributeError):
			existing = getattr(obj, p)
			if type(existing) == list:
				existing.extend(existing)
			else:
				existing = [existing]

		print(f'Setting {p}')
		identified = defaultdict(list)
		unidentified = []
		if existing:
			print('Existing value(s):')
			for v in existing:
				if hasattr(v, 'id'):
					identified[v.id].append(v)
				else:
					unidentified.append(v)
				print(f'- {v}')

		for v in values:
			print(f'Setting {p} value to {v}')
			if hasattr(v, 'id'):
				identified[v.id].append(v)
			else:
				unidentified.append(v)

		if p == 'type':
			print('*** TODO: calling setattr(_, "type") on crom objects throws an exception; skipping for now')
			return
		for i, v in identified.items():
			setattr(obj, p, None)
			setattr(obj, p, self.merge(*v))
		for v in unidentified:
			setattr(obj, p, v)

