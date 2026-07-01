# PEP 822 D-String

This repository is for verifying the impact of using [PEP 822 d-string](https://peps.python.org/pep-0822/) in real-world code.

## CPython


### Replace dedent() with d-string

[`dstringify.py`](./dstringify.py) is a script that replaces `dedent("""...""")` with `d"""..."""`.
The result of running dstringify.py on `Python/Lib` is in [`dstringify.patch`](./dstringify.patch).

The goal of this experiment is to investigate issues caused by d-string's behavior of stripping the leading newline. To reproduce that behavior faithfully, `dstringify.py` does not insert a leading blank line when replacing `dedent("""...""")`.

The result of running `make quicktest` is as follows.

```
14 tests failed:
    test__interpchannels test__interpreters test_argparse test_ast
    test_capi test_clinic test_import test_pdb test_repl test_syntax
    test_tomllib test_traceback test_unittest test_warnings
```

The first thing we can see is that much of the code does not use `dedent("""\` even though it is not necessary to have an empty line at the beginning.
Code readers can't know whether the empty line at the beginning is necessary or not, and it is a cause of technical debt.

Tests that check the line number of warnings and stacktraces fail as expected.
Let's look at the code that fails for other reasons.

```python
# Lib/test/test__interpreters.py
def _captured_script(script):
    r, w = os.pipe()
    indented = script.replace('\n', '\n                ')
    wrapped = fd"""
        import contextlib
        with open({w}, 'w', encoding="utf-8") as spipe:
            with contextlib.redirect_stdout(spipe):
                {indented}
        """
    return wrapped, open(r, encoding="utf-8")
```

This code indents the argument script and embeds it in the f-string. However, the first line of `indented` is not indented, and the second line and subsequent lines are indented with 16 spaces.

The f-string in `wrapped` indents `{indented}` with 8 spaces. Since the `script` is written with `dedent("""...""")`, the first line has an empty line, so it is not a problem. However, when using d-string for `script` in `test__interpreters.py` and `test__interpchannels.py`, the indentation is incorrect and the test fails.

This technical debt is caused by the fact that `dedent("""...""")` includes an empty line at the beginning. And this is an example of why it is beneficial for code maintainability that d-string removes the leading newline.

This function should be rewritten as follows.

```python
def _captured_script(script):
    r, w = os.pipe()
    indented = textwrap.indent(script, '        ')
    wrapped = fd"""
        import contextlib
        with open({w}, 'w', encoding="utf-8") as spipe:
            with contextlib.redirect_stdout(spipe):
        {indented}
        """
    return wrapped, open(r, encoding="utf-8")
```

A similar problem exists in `test_gc.py`. The first line of `code` is indented twice.
Rewriting `code` with d-string breaks the test because the first line is not empty.

```python
# test_gc.py
    def test_do_not_cleanup_type_subclasses_before_finalization(self):
...
        code_inside_function = Fd"""
            def test():
                {textwrap.indent(code, '    ')}

            test()
            """
        # this test checks regular garbage collection
        assert_python_ok("-c", code_inside_function)
```

Conversely, d-string's lack of a leading newline can also cause failures when a multiline fragment is concatenated onto a preceding line without a trailing newline.

The following code caused an error because `additional_code` was a one-line code snippet that did not end with a newline, and there was no empty line at the beginning of the following d-string.

```python
# import_helper.py
    if additional_code:
        script += additional_code
        script += fd"""
            if unexpected := modules_to_block & sys.modules.keys():
                after = ", ".join(unexpected)
                raise AssertionError(f'unexpectedly imported after additional code: {{after}}')
            """
```

To fix this problem, an explicit empty line needs to be added at the beginning of the d-string. Some people may feel that this is a wart, but some people may feel that it is Pythonic to explicitly show that an empty line is needed here.


### Multiline strings without dedent()

To confirm the usefulness of d-string, it is also important to see cases where dedent() is not used. [`find_multilines.py`](./find_multilines.py) is a script that finds multiline strings without dedent().
[`/multiline_literal.txt`](./multiline_literal.txt) is a list of multiline string literals without dedent().
[`/multiline_concat.txt`](./multiline_concat.txt) is a list of concatenated string lines. (e.g. `"foo\n" + "bar\n"`)

> [!NOTE]
> Some multiline literals are not directly passed to `dedent()`, but are passed to `dedent()` later.
> [`/multiline_literal.txt`](./multiline_literal.txt) contains such cases.

Of course, in most cases, `dedent()` can be used. The reason why the code writer did not use `dedent()` is only guesswork.
Maybe they didn't want to import it, or maybe they thought it was better to have fewer calls and parentheses.

There is a possibility that d-string will not be used even if it exists.
However, it is useful to confirm how d-string can be used and how it affects code readability.

test_signal.py: The code block can be written using d-string in the function arguments or list.
 When using dedent(), there is an additional function call nested in the argument list and list, so d-string is simpler.

```python
# current code

        process = subprocess.run(
                [sys.executable, "-c",
                 "import os, signal, time\n"
                 "os.kill(os.getpid(), signal.SIGINT)\n"
                 "for _ in range(999): time.sleep(0.01)"],
                stderr=subprocess.PIPE)

# with d-string

        process = subprocess.run(
                [sys.executable, "-c", d"""
                    import os, signal, time
                    os.kill(os.getpid(), signal.SIGINT)
                    for _ in range(999): time.sleep(0.01)"""],
                stderr=subprocess.PIPE)
```

test_re.py: With d-string, the position of the error is easier to understand and the test is more maintainable.
The difference between dedent() and d-string is only whether there is one leading newline and whether there is a nested function call.

```python
# current

        # Multiline pattern
        with self.assertRaises(re.PatternError) as cm:
            re.compile("""
                (
                    abc
                )
                )
                (
                """, re.VERBOSE)
        err = cm.exception
        self.assertEqual(err.pos, 77)
        self.assertEqual(err.lineno, 5)
        self.assertEqual(err.colno, 17)

# d-string

        # Multiline pattern
        with self.assertRaises(re.PatternError) as cm:
            re.compile(d"""
                (
                    abc
                )
                )
                (
                """, re.VERBOSE)
        err = cm.exception
        self.assertEqual(err.pos, 12)
        self.assertEqual(err.lineno, 4)
        self.assertEqual(err.colno, 1)
```

datetimetester.py: It uses `if True:` hack to avoid using `dedent()`. This hack is not necessary if d-string is used.
The readability benefit is minimal compared to `dedent()`, but it is more efficient to process at compile time compared to processing after the f-string is executed.

```python
# current
    def test_static_datetime_types_outlive_collected_module(self, setup, call):
        # gh-151039: This code used to crash
        script = f"""if True:
            import sys, gc
            import _datetime

            {setup}                          # static C type, survives the module
            del sys.modules['_datetime']
            del _datetime
            sys.modules['_datetime'] = None  # block re-import
            gc.collect()                     # module object is collected

            try:
                {call}                       # used to be a segmentation fault
            except ImportError:
                pass
            else:
                raise AssertionError("ImportError not raised")
        """

# with d-string
    def test_static_datetime_types_outlive_collected_module(self, setup, call):
        # gh-151039: This code used to crash
        script = df"""
            import sys, gc
            import _datetime

            {setup}                          # static C type, survives the module
            del sys.modules['_datetime']
            del _datetime
            sys.modules['_datetime'] = None  # block re-import
            gc.collect()                     # module object is collected

            try:
                {call}                       # used to be a segmentation fault
            except ImportError:
                pass
            else:
                raise AssertionError("ImportError not raised")
            """
```

test_codecencodings_cn.py: There are some multiline byte strings. They can be written using bd-string, while `dedent()` doesn't support bytes.

```python
# current
    codectests = (
        # test '~\n' (3 lines)
        (b'This sentence is in ASCII.\n'
         b'The next sentence is in GB.~{<:Ky2;S{#,~}~\n'
         b'~{NpJ)l6HK!#~}Bye.\n',
         'strict',
         'This sentence is in ASCII.\n'
         'The next sentence is in GB.'
         '\u5df1\u6240\u4e0d\u6b32\uff0c\u52ff\u65bd\u65bc\u4eba\u3002'
         'Bye.\n'),

# d-string
    codectests = (
        # test '~\n' (3 lines)
        (bd"""
         This sentence is in ASCII.
         The next sentence is in GB.~{<:Ky2;S{#,~}~
         ~{NpJ)l6HK!#~}Bye.
         """,
         'strict',
         d"""
         This sentence is in ASCII.
         The next sentence is in GB.\
         \u5df1\u6240\u4e0d\u6b32\uff0c\u52ff\u65bd\u65bc\u4eba\u3002\
         'Bye.
         """),
```

test_traceback.py: There are several multiline strings that have indentation.
They are good examples of how d-string can be used to write multiline indented strings.
When using dedent(), additional indent() is needed.

```python
# current

        expected = ('  | ExceptionGroup: eg999 (3 sub-exceptions)\n'
                    '  +-+---------------- 1 ----------------\n'
                    '    | ValueError: 999\n'
                    '    +---------------- 2 ----------------\n'
                    '    | ExceptionGroup: eg998 (3 sub-exceptions)\n'
                    '    +-+---------------- 1 ----------------\n'
                    '      | ValueError: 998\n'
                    '      +---------------- 2 ----------------\n'
...
                    '      +---------------- 3 ----------------\n'
                    '      | ValueError: -998\n'
                    '      +------------------------------------\n'
                    '    +---------------- 3 ----------------\n'
                    '    | ValueError: -999\n'
                    '    +------------------------------------\n')

# d-string

        expected = d'''
          | ExceptionGroup: eg999 (3 sub-exceptions)
          +-+---------------- 1 ----------------
            | ValueError: 999
            +---------------- 2 ----------------
            | ExceptionGroup: eg998 (3 sub-exceptions)
            +-+---------------- 1 ----------------
              | ValueError: 998
              +---------------- 2 ----------------
...
              +---------------- 3 ----------------
              | ValueError: -998
              +------------------------------------
            +---------------- 3 ----------------
            | ValueError: -999
            +------------------------------------
        '''
```

## Other projects

The great benefit of d-string is that it can be used together with t-string, but there are few projects that support t-string, and it is difficult to find combinations of t-string and long multiline strings.

For example, [psycopg2](https://www.psycopg.org/psycopg3/docs/basic/tstrings.html) supports t-string, but [test_tstring.py](https://github.com/psycopg/psycopg/blob/master/tests/test_tstring.py) only contains one line of SQL. Maybe, multiline t-strings are used in closed-source applications rather than open-source libraries like psycopg2.

I will also investigate other projects in the same way as CPython. But CPython test code includes massive amount of multiline strings; Python, JSON, HTML, HTTP, SMTP, etc...
I haven't found a way to find patterns that are not included in the Python source code yet.
