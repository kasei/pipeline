#!/usr/bin/env python3 -B

import re
import os
import sys
import json
import time
import uuid
import pprint
import itertools
from pathlib import Path

from settings import output_file_path
from pipeline.util.rewriting import rewrite_output_files, JSONValueRewriter

if __name__ == '__main__':
	if len(sys.argv) < 2:
		cmd = sys.argv[0]
		print(f'''
	Usage: {cmd} URI_REWRITE_MAP.json

		'''.lstrip())
		sys.exit(1)

	rewrite_map_filename = sys.argv[1]

	kwargs = {}
	if len(sys.argv) > 2:
		kwargs['files'] = sys.argv[2:]

	print(f'Rewriting post-sales URIs ...')
	start_time = time.time()
	with open(rewrite_map_filename, 'r') as f:
		post_sale_rewrite_map = json.load(f)
	# 	print('Post sales rewrite map:')
	# 	pprint.pprint(post_sale_rewrite_map)
		r = JSONValueRewriter(post_sale_rewrite_map, prefix=True)
		prefix = os.path.commonprefix(list(post_sale_rewrite_map.keys()))
		if len(prefix) > 20:
			kwargs['content_filter_re'] = re.compile(re.escape(prefix))
		rewrite_output_files(r, parallel=True, concurrency=8, **kwargs)
	cur = time.time()
	elapsed = cur - start_time
	print(f'Done (%.1fs)' % (elapsed,))
