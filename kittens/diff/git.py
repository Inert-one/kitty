#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

import concurrent.futures
import os
import subprocess


def run_diff(file1, file2, context=3):
    # returns: ok, is_different, patch
    p = subprocess.Popen([
        'git', 'diff', '--no-color', '--no-ext-diff', '--exit-code', '-U' + str(context), '--no-index', '--'
        ] + [file1, file2],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    stdout, stderr = p.communicate()
    returncode = p.wait()
    if returncode in (0, 1):
        return True, returncode == 1, stdout.decode('utf-8')
    return False, returncode, stderr.decode('utf-8')


class Chunk:

    __slots__ = ('is_context', 'left_start', 'right_start', 'left_count', 'right_count')

    def __init__(self, left_start, right_start, is_context=False):
        self.is_context = is_context
        self.left_start = left_start
        self.right_start = right_start
        self.left_count = self.right_count = 0

    def add_line(self):
        self.right_count += 1

    def remove_line(self):
        self.left_count += 1

    def context_line(self):
        self.left_count += 1
        self.right_count += 1

    def __repr__(self):
        return 'Chunk(is_context={}, left_start={}, left_count={}, right_start={}, right_count={})'.format(
                self.is_context, self.left_start, self.left_count, self.right_start, self.right_count)


class Hunk:

    def __init__(self, title, left, right):
        self.left_start, self.left_count = left
        self.right_start, self.right_count = right
        self.left_start -= 1  # 0-index
        self.right_start -= 1  # 0-index
        self.title = title
        self.chunks = []
        self.current_chunk = None
        self.largest_line_number = max(self.left_start + self.left_count, self.right_start + self.right_count)

    def new_chunk(self, is_context=False):
        if self.chunks:
            c = self.chunks[-1]
            left_start = c.left_start + c.left_count
            right_start = c.right_start + c.right_count
        else:
            left_start = self.left_start
            right_start = self.right_start
        return Chunk(left_start, right_start, is_context)

    def ensure_diff_chunk(self):
        if self.current_chunk is None:
            self.current_chunk = self.new_chunk(is_context=False)
        elif self.current_chunk.is_context:
            self.chunks.append(self.current_chunk)
            self.current_chunk = self.new_chunk(is_context=False)

    def ensure_context_chunk(self):
        if self.current_chunk is None:
            self.current_chunk = self.new_chunk(is_context=True)
        elif not self.current_chunk.is_context:
            self.chunks.append(self.current_chunk)
            self.current_chunk = self.new_chunk(is_context=True)

    def add_line(self):
        self.ensure_diff_chunk()
        self.current_chunk.add_line()

    def remove_line(self):
        self.ensure_diff_chunk()
        self.current_chunk.remove_line()

    def context_line(self):
        self.ensure_context_chunk()
        self.current_chunk.context_line()

    def finalize(self):
        self.chunks.append(self.current_chunk)
        del self.current_chunk
        # Sanity check
        c = self.chunks[-1]
        if c.left_start + c.left_count != self.left_start + self.left_count:
            raise ValueError('Left side line mismatch {} != {}'.format(c.left_start + c.left_count, self.left_start + self.left_count))
        if c.right_start + c.right_count != self.right_start + self.right_count:
            raise ValueError('Left side line mismatch {} != {}'.format(c.right_start + c.right_count, self.right_start + self.right_count))


def parse_range(x):
    parts = x[1:].split(',', 1)
    start = abs(int(parts[0]))
    count = 1 if len(parts) < 2 else int(parts[1])
    return start, count


def parse_hunk_header(line):
    parts = tuple(filter(None, line.split('@@', 2)))
    linespec = parts[0].strip()
    title = ''
    if len(parts) == 2:
        title = parts[1].strip()
    left, right = map(parse_range, linespec.split())
    return Hunk(title, left, right)


class Patch:

    def __init__(self, all_hunks):
        self.all_hunks = all_hunks
        self.largest_line_number = self.all_hunks[-1].largest_line_number if self.all_hunks else 0

    def __iter__(self):
        return iter(self.all_hunks)

    def __len__(self):
        return len(self.all_hunks)


def parse_patch(raw):
    all_hunks = []
    for line in raw.splitlines():
        if line.startswith('@@ '):
            current_hunk = parse_hunk_header(line)
            all_hunks.append(current_hunk)
        else:
            if not all_hunks:
                continue
            q = line[0]
            if q == '+':
                all_hunks[-1].add_line()
            elif q == '-':
                all_hunks[-1].remove_line()
            else:
                all_hunks[-1].context_line()
    for h in all_hunks:
        h.finalize()
    return Patch(all_hunks)


class Differ:

    def __init__(self):
        self.jmap = {}
        self.jobs = []

    def add_diff(self, file1, file2):
        self.jmap[file1] = file2
        self.jobs.append(file1)

    def __call__(self, context=3):
        ans = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
            jobs = {executor.submit(run_diff, key, self.jmap[key], context): key for key in self.jobs}
            for future in concurrent.futures.as_completed(jobs):
                key = jobs[future]
                try:
                    ok, returncode, output = future.result()
                except FileNotFoundError as err:
                    return 'Could not find the {} executable. Is it in your PATH?'.format(err.filename)
                except Exception as e:
                    return 'Running git diff for {} vs. {} generated an exception: {}'.format(key[0], key[1], e)
                if not ok:
                    return output + '\nRunning git diff for {} vs. {} failed'.format(key[0], key[1])
                try:
                    patch = parse_patch(output)
                except Exception:
                    import traceback
                    return traceback.format_exc() + '\nParsing diff for {} vs. {} failed'.format(key[0], key[1])
                else:
                    ans[key] = patch
        return ans
