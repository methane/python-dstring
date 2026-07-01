# PEP 822 D-String

This repository is for verifying the impact of using [PEP 822 d-string](https://peps.python.org/pep-0822/) in real-world code.

## CPython


### Replace dedent() with dstring

`/dstringify.py` is a script that replaces `dedent("""...""")` with `d"""..."""`.
The result of running dstringify.py on `Python/Lib` is in `/dstringify.patch`.

The goals of this attempt is to verify the problem caused by the specification of d-string that strips the leading newline.
Therefore, it does not add an empty line at the beginning when replacing.

The result of running `make quicktest` is as follows.

```
14 tests failed:
    test__interpchannels test__interpreters test_argparse test_ast
    test_capi test_clinic test_import test_pdb test_repl test_syntax
    test_tomllib test_traceback test_unittest test_warnings
```

The first thing we can see is that many code does not use `dedent("""...""")` even though it is not necessary to have an empty line at the beginning.
This is because we do not know whether the empty line at the beginning is necessary or not, or whether it is because we do not want to write `\` for some reason, and it is a cause of technical debt.

Tests that check the line number of warning and stacktrace fail as expected.
Let's look at the code that fails by other reasons.


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

This code indents the argument script and embeds it in the f-string. However, the first line of `indented` is not indented, and the second line and subsequent lines are indented with 16-space.

The f-string in `wrapped` indents `{indented}` with 8-space. If the script is written with `dedent("""...""")`, the first line has an empty line, so it is not a problem. However, if d-string is used, the first line is indented with 8-space, and the second line and subsequent lines are indented with 16-space, so the indentation is incorrect and the test fails.

This behavior is not a technical debt, but a technical debt because it is a technical debt that happens to work by chance. When someone adds a test, if they use `dedent("""...""")`, they will unintentionally encounter an indentation error.

When embedding an indented string in an f-string, it should be done as follows.

```python
def _captured_script(script):
    r, w = os.pipe()
    indented = textwrap.indent(script, '                ')
    wrapped = fd"""
        import contextlib
        with open({w}, 'w', encoding="utf-8") as spipe:
            with contextlib.redirect_stdout(spipe):
        {indented}
        """
    return wrapped, open(r, encoding="utf-8")
```

The same problem exists in `test_gc.py`.

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

However, there is also a problem that occurs because there is no empty line at the beginning of d-string.

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

To confirm the usefulness of d-string, it is also important to see cases where dedent() is not used. `/find_multilines.py` is a script that finds multiline strings without dedent().
`/multiline_literal.txt` is list of multiline string literals without dedent(). `/multiline_concat.txt` is list of concatinated string lines. (e.g. `"foo\n" + "bar\n"`)

Of course, in most cases, `dedent()` can be used. The reason why the code writer did not use `dedent()` is only guesswork.
Maybe they didn't want to import it, or maybe they thought it was better to have fewer calls and parentheses.

There is a possibility that d-string will not be used even if it exists.
However, it is useful to confirm how d-string can be used and it affects code readability.

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
The difference between dedent() and d-string is only whether there is one leading newline and whether there is a function call nested.

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
The readability benefit is almost none compared to dedent(), but it is more efficient to process at compile time compared to processing after the f-string is executed.

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

test_codecencodings_cn.py: There are some multiline bytes. They can be written using d-string, while `dedent()` doesn't support bytes.

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
#current code

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

For example, [psycopg2](https://www.psycopg.org/psycopg3/docs/basic/tstrings.html) supports t-string, but [test_tstring.py](https://github.com/psycopg/psycopg/blob/master/tests/test_tstring.py) only contains one line of SQL. The places where d-string can be useful are not open source libraries, but closed source applications.

I will also investigate other projects in the same way as CPython, but in most cases, it will be used in test code like CPython.

