from numba import cuda, float32, int32
from numba.core.errors import NumbaInvalidConfigWarning
from numba.cuda.testing import CUDATestCase, skip_on_cudasim
from numba.cuda.cudadrv.nvvm import NVVM
import re
import unittest
import warnings


@skip_on_cudasim('Simulator does not produce lineinfo')
class TestCudaLineInfo(CUDATestCase):
    def _loc_directive_regex(self):
        # This is used in several tests

        pat = (
            r'\.loc'      # .loc directive beginning
            r'\s+[0-9]+'  # whitespace then file index
            r'\s+[0-9]+'  # whitespace then line number
            r'\s+[0-9]+'  # whitespace then column position
        )
        return re.compile(pat)

    def _check(self, fn, sig, expect):
        fn.compile(sig)
        llvm = fn.inspect_llvm(sig)
        ptx = fn.inspect_asm(sig)
        assertfn = self.assertIsNotNone if expect else self.assertIsNone

        # DICompileUnit debug info metadata should all be of the
        # DebugDirectivesOnly kind, and not the FullDebug kind
        pat = (
            r'!DICompileUnit\(.*'    # Opening of DICompileUnit metadata. Since
                                     # the order of attributes is not
                                     # guaranteed, we need to match arbitrarily
                                     # afterwards.
            r'emissionKind:\s+'      # The emissionKind attribute followed by
                                     # whitespace.
            r'DebugDirectivesOnly'   # The correct emissionKind.
        )
        match = re.compile(pat).search(llvm)
        assertfn(match, msg=ptx)

        pat = (
            r'!DICompileUnit\(.*'  # Same as the pattern above, but for the
            r'emissionKind:\s+'    # incorrect FullDebug emissionKind.
            r'FullDebug'           #
        )
        match = re.compile(pat).search(llvm)
        self.assertIsNone(match, msg=ptx)

        # The name of this file should be present in the line mapping
        # if lineinfo was propagated through correctly.
        pat = (
            r'\.file'                # .file directive beginning
            r'\s+[0-9]+\s+'          # file number surrounded by whitespace
            r'".*test_lineinfo.py"'  # filename in quotes, ignoring full path
        )
        match = re.compile(pat).search(ptx)
        assertfn(match, msg=ptx)

        # .loc directives should be present in the ptx
        self._loc_directive_regex().search(ptx)
        assertfn(match, msg=ptx)

        # Debug info sections should not be present when only lineinfo is
        # generated
        pat = (
            r'\.section\s+'  # .section directive beginning
            r'\.debug_'      # Any section name beginning ".debug_"
        )
        match = re.compile(pat).search(ptx)
        self.assertIsNone(match, msg=ptx)

    def test_no_lineinfo_in_asm(self):
        @cuda.jit(lineinfo=False)
        def foo(x):
            x[0] = 1

        self._check(foo, sig=(int32[:],), expect=False)

    def test_lineinfo_in_asm(self):
        if not NVVM().is_nvvm70:
            self.skipTest("lineinfo not generated for NVVM 3.4")

        @cuda.jit(lineinfo=True)
        def foo(x):
            x[0] = 1

        self._check(foo, sig=(int32[:],), expect=True)

    def test_lineinfo_maintains_error_model(self):
        sig = (float32[::1], float32[::1])

        @cuda.jit(sig, lineinfo=True)
        def divide_kernel(x, y):
            x[0] /= y[0]

        llvm = divide_kernel.inspect_llvm(sig)

        # When the error model is Python, the device function returns 1 to
        # signal an exception (e.g. divide by zero) has occurred. When the
        # error model is the default NumPy one (as it should be when only
        # lineinfo is enabled) the device function always returns 0.
        self.assertNotIn('ret i32 1', llvm)

    def test_no_lineinfo_in_device_function(self):
        # Ensure that no lineinfo is generated in device functions by default.
        @cuda.jit
        def callee(x):
            x[0] += 1

        @cuda.jit
        def caller(x):
            x[0] = 1
            callee(x)

        sig = (int32[:],)
        self._check(caller, sig=sig, expect=False)

    def test_lineinfo_in_device_function(self):
        if not NVVM().is_nvvm70:
            self.skipTest("lineinfo not generated for NVVM 3.4")

        # First we define a device function / kernel pair and run the usual
        # checks on the generated LLVM and PTX.

        @cuda.jit(lineinfo=True)
        def callee(x):
            x[0] += 1

        @cuda.jit(lineinfo=True)
        def caller(x):
            x[0] = 1
            callee(x)

        sig = (int32[:],)
        self._check(caller, sig=sig, expect=True)

        # Now we can check the PTX of the device function specifically.

        ptx = caller.inspect_asm(sig)
        ptxlines = ptx.splitlines()

        # To check the device function, we need to identify its boundaries.

        # A line beginning with ".weak .func"
        devfn_start = re.compile(r'^\.weak\s*\.func')

        # Identify the beginning of the function.
        start = None

        for lineno, line in enumerate(ptxlines):
            if devfn_start.match(line) is not None:
                # We will begin our search on the line following the
                # declaration
                start = lineno + 1

        if start is None:
            self.fail(f'Could not identify device function in:\n\n{ptx}')

        # Identify the end of the function
        end = None

        for offset, line in enumerate(ptxlines[start:]):
            # Assume the end of the function is a line with an unindented '}'
            if line[:1] == '}':
                end = start + offset

        if end is None:
            self.fail(f'Could not identify end of device function in:\n\n{ptx}')

        # Scan for .loc directives in the device function.
        loc_directive = self._loc_directive_regex()
        found = False

        for line in ptxlines[start:end]:
            if loc_directive.search(line) is not None:
                found = True

        if not found:
            # Join one line either side so the function as a whole is shown,
            # i.e. including the declaration and parameter list, and the
            # closing brace.
            devfn = "\n".join(ptxlines[start - 1:end + 1])
            self.fail(f'.loc directive not found in:\n\n{devfn}')

        # We also inspect the LLVM to ensure that there's debug info for each
        # subprogram (function). A lightweight way to check this is to ensure
        # that we have as many DISubprograms as we expect.

        llvm = caller.inspect_llvm(sig)
        subprograms = 0
        for line in llvm.splitlines():
            if 'distinct !DISubprogram' in line:
                subprograms += 1

        # One DISubprogram for each of:
        # - The kernel wrapper
        # - The caller
        # - The callee
        expected_subprograms = 3

        self.assertEqual(subprograms, expected_subprograms,
                         f'"Expected {expected_subprograms} DISubprograms; '
                         f'got {subprograms}')

    def test_debug_and_lineinfo_warning(self):
        with warnings.catch_warnings(record=True) as w:
            # We pass opt=False to prevent the warning about opt and debug
            # occurring as well
            @cuda.jit(debug=True, lineinfo=True, opt=False)
            def f():
                pass

        self.assertEqual(len(w), 1)
        self.assertEqual(w[0].category, NumbaInvalidConfigWarning)
        self.assertIn('debug and lineinfo are mutually exclusive',
                      str(w[0].message))


if __name__ == '__main__':
    unittest.main()
