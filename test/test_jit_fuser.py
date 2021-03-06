from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import functools
import os
import unittest
import sys
import torch
import torch.autograd.function as function
from torch import Tensor

from common_utils import TestCase, run_tests, IS_WINDOWS, \
    skipIfRocm, IS_SANDCASTLE
from typing import List, Dict, Optional, Tuple

from test_jit import JitTestCase, enable_cpu_fuser, RUN_CUDA, RUN_CUDA_HALF, RUN_CUDA_MULTI_GPU, \
    backward_graph


class TestFuser(JitTestCase):
    def assertAllFused(self, graph, except_for=()):
        if [n.kind() for n in graph.nodes()] == ['prim::DifferentiableGraph']:
            graph = next(graph.nodes()).g('Subgraph')
        allowed_nodes = {'prim::Constant', 'prim::FusionGroup'} | set(except_for)
        self.assertTrue(all(node.kind() in allowed_nodes for node in graph.nodes()),
                        'got {}'.format(graph))
        self.assertTrue([node.kind() for node in graph.nodes()].count('prim::FusionGroup') == 1)

    def _test_fused_abs(self, device='cpu'):

        @torch.jit.script
        def func(x):
            return x.abs() * 2

        a = torch.randn(5, device=device)
        self.assertEqual(func(a), a.abs() * 2)
        self.assertAllFused(func.graph_for(a))

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_abs_cpu(self):
        self._test_fused_abs()

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "requires CUDA")
    @skipIfRocm
    def test_abs_cuda(self):
        self._test_fused_abs(device="cuda")

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_arg_configurations_smoke_cuda(self):
        # A smoke test to make sure we won't use the same kernel for contiguous
        # and non-contiguous arguments.
        # TODO: add optionally enabled debug counters to the fuser to verify
        #       that we really can tell the difference between configurations
        def f(x, y):
            z1, z2 = (x + y).chunk(2, dim=1)
            return z1 * z2

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        traced_f = torch.jit.trace(f, (x, y,))
        self.assertEqual(traced_f(x.t().contiguous(), y), traced_f(x.t(), y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_broadcast_cuda(self):
        def scaleshift(x, scale, shift):
            return x * scale + shift

        inputs = [
            torch.randn(4, 4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
        ]
        ge = self.checkTrace(scaleshift, inputs)
        self.assertAllFused(ge.graph_for(*inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_HALF, "no half support")
    def test_cuda_half(self):
        x = torch.randn(4, 4, dtype=torch.half, device='cuda')
        y = torch.randn(4, 4, dtype=torch.half, device='cuda')

        funcs = [
            self.fn_test_comparison_gt_lt,
            self.fn_test_relu,
            self.fn_test_exp
        ]

        # Note: Non fused inputs must be float to prevent loss of precision
        inputs = (x.float(), y.float())
        fusion_inputs = (x, y)
        for fn in funcs:
            local_inputs = [t.clone().requires_grad_() for t in inputs]
            local_fusion_inputs = [t.clone().requires_grad_() for t in fusion_inputs]

            # Verifies outputs
            fusion = torch.jit.trace(fn, local_fusion_inputs, check_trace=False, optimize=True)
            outputs = fn(*local_inputs)
            fusion_outputs = fusion(*local_fusion_inputs)
            outputs_half = [t.half() for t in outputs]
            self.assertEqual(outputs_half, fusion_outputs)

            # Verifies gradients
            for output, fusion_output in zip(outputs_half, fusion_outputs):
                grads = torch.autograd.grad(
                    output.float().sum(), local_inputs, allow_unused=True, retain_graph=True)
                fusion_grads = torch.autograd.grad(
                    fusion_output.sum(), local_fusion_inputs, allow_unused=True, retain_graph=True)
                grads_half = [t.half() for t in grads]
                self.assertEqual(grads_half, fusion_grads)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_checks_cat_inputs(self):
        # We shouldn't treat cat nodes as broadcasting. All their inputs
        # need to be checked for having the same map size, before we can
        # run the kernel.
        @torch.jit.script
        def f(x, y):
            return torch.cat([x + 2 * x + x ** 2, y + 4 * y + y ** 3], dim=0)

        # NOTE: y is broadcastable to x, but output of f(x, y) should have
        # shape 3x4, and not 4x4.
        x = torch.randn(2, 4, dtype=torch.float, device='cuda')
        y = torch.randn(1, 4, dtype=torch.float, device='cuda')

        self.assertEqual(f(x, y).shape, (3, 4))
        self.assertAllFused(f.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_chunk_cuda(self):
        def fn(x):
            a, b, c = x.chunk(3, 1)
            return a * b + c

        inputs = [torch.randn(10, 6, dtype=torch.float, device='cuda')]

        ge = self.checkScript(fn, inputs)
        graph = ge.graph_for(*inputs)
        self.assertAllFused(graph)
        FileCheck().check("prim::ConstantChunk[chunks=3, dim=1]").run(str(graph))

    @staticmethod
    def _test_chunk_correctness(self, device='cpu'):
        def chunk_4_0(x):
            x0, x1, x2, x3 = x.chunk(4, 0)
            return x0 + x1 + x2 + x3

        def chunk_4_1(x):
            x0, x1, x2, x3 = x.chunk(4, 1)
            return x0 + x1 + x2 + x3

        def chunk_4_last(x):
            x0, x1, x2, x3 = x.chunk(4, 2)
            return x0 + x1 + x2 + x3

        fns = [chunk_4_0, chunk_4_1, chunk_4_last]
        tensors = [
            # splitSize = 1
            torch.randn(4, 4, 4, dtype=torch.float, device=device),

            # contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device),

            # non-contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device).transpose(1, 2),
        ]

        for tensor in tensors:
            for fn in fns:
                self.checkScript(fn, [tensor])

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_chunk_correctness(self):
        return self._test_chunk_correctness(self, 'cpu')

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    def test_chunk_correctness_cuda(self):
        return self._test_chunk_correctness(self, 'cuda')

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_chunk_distributes_cuda(self):
        def f(x, y):
            z1, z2 = (x + y).chunk(2, dim=1)
            return z1 * z2

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        graph = ge.graph_for(x, y)
        FileCheck().check("broadcast_tensors").check('with prim::FusionGroup_0') \
            .check_count('ConstantChunk', 2, exactly=True).run(str(graph))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_chunk_motion_deduplicates_inputs(self):
        def func1(x):
            z = x * x
            z0, z1 = z.chunk(2)
            return z0 * z1

        def func2(x):
            z = x * x * x
            z0, z1 = z.chunk(2)
            return z0 * z1

        inputs = [
            torch.tensor([1.1, 1.2], device='cuda', dtype=torch.float),
        ]
        for func in [func1, func2]:
            module = self.checkScript(func, inputs)
            forward_graph = module.graph_for(*inputs)
            self.assertGraphContainsExactly(forward_graph, 'prim::FusionGroup', 1)
            fusion_group = list(forward_graph.nodes())[-1]
            self.assertEqual(len(list(fusion_group.inputs())), 1)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_chunk_multiple_cuda(self):
        # The arguments are intentionally used out of order as a test to see
        # if the fusion compiler adds extra args in the correct order
        def fn(s, x, y, z):
            z1, z2 = z.chunk(2, 2)
            x1, x2, x3 = x.chunk(3, 1)
            y1, y2 = y.chunk(2, 0)
            return s + x1 + x2 + x3 + y1 + y2 + z1 + z2

        inputs = [
            torch.randn(5, 2, 3, dtype=torch.float, device='cuda'),
            torch.randn(5, 6, 3, dtype=torch.float, device='cuda'),
            torch.randn(10, 2, 3, dtype=torch.float, device='cuda'),
            torch.randn(5, 2, 6, dtype=torch.float, device='cuda'),
        ]

        ge = self.checkScript(fn, inputs)
        self.assertAllFused(ge.graph_for(*inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_clamp(self):
        def func2(a, b):
            return torch.clamp(a + b, min=0, max=2)

        def funcInf(a, b):
            return torch.clamp(a + b, min=0, max=float('inf'))

        def funcOptMin(a, b):
            return torch.clamp(a + b, max=2)

        def funcOptMax(a, b):
            return torch.clamp(a + b, min=0)

        a = torch.randn(4, 4, dtype=torch.float, device='cuda', requires_grad=True)
        b = torch.randn(4, 4, dtype=torch.float, device='cuda')
        nan = torch.tensor(float('nan'))

        funcs = (func2, funcInf, funcOptMin, funcOptMax)
        for f, inputs in product(funcs, [[a, b], [a, nan]]):
            inp1, inp2 = inputs
            s = self.checkScript(f, (inp1, inp2))
            self.assertAllFused(s.graph_for(inp1, inp2), except_for={'aten::size'})

            c = s(inp1, inp2)
            c.sum().backward()
            graph = backward_graph(s)
            self.assertAllFused(graph)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_eq_ne(self):
        def f(x, y):
            mask = (x == 0).type_as(x)
            z = x * mask + y
            mask = (x != 0).type_as(x)
            z = z * mask + y
            return z

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @staticmethod
    def fn_test_comparison_gt_lt(x, y):
        mask = (x > 0).type_as(x)
        z = x * mask + y
        mask = (x < 0).type_as(x)
        z = z * mask + y
        return z

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_gt_lt_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_comparison_gt_lt, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_ge_le_cuda(self):
        def f(x, y):
            mask = (x >= 0).type_as(x)
            z = x * mask + y
            mask = (x <= 0).type_as(x)
            z = z * mask + y
            return z

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        self.assertAllFused(ge.graph_for(x, y))
        x.requires_grad_(True)
        y.requires_grad_(True)
        self.assertAllFused(ge.graph_for(x, y), except_for=("aten::size", "prim::BroadcastSizes"))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_addcmul_cuda(self):
        t = torch.randn(1, 4, dtype=torch.float, device='cuda')
        t1 = torch.randn(4, 1, dtype=torch.float, device='cuda')
        t2 = torch.randn(1, 4, dtype=torch.float, device='cuda')

        def foo(t, t1, t2):
            return t.addcmul(t + 1, t2, value=0.1)

        ge = self.checkTrace(foo, (t, t1, t2), allow_unused=True)
        graph = ge.graph_for(t, t1, t2)
        self.assertAllFused(graph)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lerp_cuda(self):
        start = torch.randn(4, 1, dtype=torch.float, device='cuda')
        end = torch.randn(1, 4, dtype=torch.float, device='cuda')
        weight = torch.tensor(0.5, dtype=torch.float, device='cuda')

        # scalar weight overload
        def foo_weight_scalar(start, end):
            return torch.lerp(start + 1, end, 0.5)

        # tensor weight overload
        def foo_weight_tensor(start, end):
            return torch.lerp(start + 1, end, weight)

        ge_weight_scalar = self.checkTrace(foo_weight_scalar, (start, end))
        graph = ge_weight_scalar.graph_for(start, end)
        self.assertAllFused(graph)

        ge_weight_tensor = self.checkTrace(foo_weight_tensor, (start, end))
        graph = ge_weight_tensor.graph_for(start, end)
        self.assertAllFused(graph)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_concat_cuda(self):
        hx = torch.randn(3, 20, dtype=torch.float, device='cuda')
        cx = torch.randn(3, 20, dtype=torch.float, device='cuda')

        def foo(hx, cx):
            return torch.cat((hx + cx, hx * cx))

        ge = self.checkTrace(foo, (hx, cx))
        graph = ge.graph_for(hx, cx)
        self.assertAllFused(graph)
        FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_concat_invariant_cuda(self):
        # Invariant: the output of prim::FusedConcat may
        # not be an input to any node inside the FusionGroup.
        def fn(x, y, z):
            x1 = x + y
            y1 = x - y
            w = torch.cat([x1, y1])
            return w + z

        x = torch.randn(2, 2, dtype=torch.float, device='cuda')
        y = torch.randn(2, 2, dtype=torch.float, device='cuda')
        z = torch.randn(4, 2, dtype=torch.float, device='cuda')
        ge = self.checkTrace(fn, (x, y, z))
        graph = ge.graph_for(x, y, z)
        self.assertAllFused(graph, except_for={'aten::add'})
        FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    @staticmethod
    def fn_test_exp(x, y):
        return (x + .5 * y).exp()

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_exp_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_exp, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_fuse_batch_norm(self):

        class ResLike(torch.jit.ScriptModule):
            def __init__(self, optimize=True):
                super(ResLike, self).__init__(optimize)
                self.bn = nn.BatchNorm2d(16)

            @torch.jit.script_method
            def forward(self, x, y):
                return y + torch.relu(self.bn(x))

        model = ResLike().cuda()
        model_noopt = ResLike(optimize=False).cuda()
        model_noopt.load_state_dict(model.state_dict())
        x = torch.randn(2, 16, 8, 8, device='cuda')
        y = torch.randn(2, 16, 8, 8, device='cuda')
        # FIXME: We need differentiation for CNNs for this optimization to trigger
        with torch.no_grad():
            out = model(x, y)
            graph = model.graph_for(x, y)
            rep = str(graph)

            out_noopt = model_noopt(x, y)
            rep_noopt = str(model_noopt.graph_for(x, y))
            self.assertEqual(out, out_noopt, prec=3e-5)

        # Check that batch_norm has really been decomposed
        self.assertIn('aten::batch_norm_update_stats', rep)
        self.assertNotIn('aten::batch_norm(', rep)
        self.assertIn('aten::batch_norm(', rep_noopt)

        # Make sure the fusion group is big, and contains aten::sqrt, which could
        # originate only from decomposing batch_norm in this case
        fusion_groups = [node for node in graph.nodes() if node.kind() == 'prim::FusionGroup']
        self.assertEqual(len(fusion_groups), 1)
        fused_graph = fusion_groups[0].g('Subgraph')
        self.assertTrue(any(node.kind() == 'aten::sqrt' for node in fused_graph.nodes()))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_threshold(self):
        def f(x):
            return torch.threshold(x, 0, -10) + x + x + x

        x = torch.tensor([-1, -0.5, 0, 1, 2, 3], device='cuda')
        scripted = torch.jit.script(f)

        self.assertEqual(f(x), scripted(x))
        self.assertAllFused(scripted.graph_for(x))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_scalar_arg_cuda(self):
        def fn_test_scalar_arg(x, p):
            # type: (Tensor, float) -> Tensor
            return p * (x * x + x)

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        p = 3
        scripted = torch.jit.script(fn_test_scalar_arg, (x, p))
        self.assertEqual(fn_test_scalar_arg(x, p), scripted(x, p))
        self.assertAllFused(scripted.graph_for(x, p))
        x.requires_grad_(True)
        out = scripted(x, p)
        self.assertAllFused(scripted.graph_for(x, p), except_for=("aten::size", "prim::BroadcastSizes"))

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_fuser_deduplication(self):
        # See that fusion kernel outputs are deduplicated when removing  _grad_sum_to_size in the fuser's compilation
        # see the discussion in PR #14957.
        def f(x, y):
            return torch.sigmoid(x + y)

        b = torch.randn(5, 5, requires_grad=True)
        a = torch.randn(5, 5, requires_grad=True)
        s = self.checkScript(f, (a, b))
        self.assertAllFused(s.graph_for(a, b), except_for={'aten::size'})

        c = s(a, b)
        ga, gb = torch.autograd.grad(c.sum(), [a, b])
        graph = backward_graph(s)
        self.assertAllFused(graph)
        # check that a, b share storage, i.e. were generated as a single output in the fuser
        self.assertEqual(ga.data_ptr(), gb.data_ptr())

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_fuser_iou(self):
        # This checks if most of Intersection over Union is fused.
        # In particular, the backward contains many _grad_sum_to_size.
        def iou(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2):
            ltx = torch.max(b1x1, b2x1)  # [N,M]
            lty = torch.max(b1y1, b2y1)
            rbx = torch.min(b1x2, b2x2)
            rby = torch.min(b1y2, b2y2)

            w = (rbx - ltx).clamp(min=0, max=float('inf'))  # [N,M]
            h = (rby - lty).clamp(min=0, max=float('inf'))  # [N,M]
            inter = w * h  # [N,M]

            area1 = (b1x2 - b1x1) * (b1y2 - b1y2)  # [N,1]
            area2 = (b2x2 - b2x1) * (b2y2 - b2y2)  # [1,M]
            iou = inter / (area1 + area2 - inter)
            return iou

        box1 = torch.randn(5, 4, requires_grad=True)
        box2 = torch.randn(5, 4, requires_grad=True)
        # unsqueezing can currently not be fused
        b1x1 = box1[:, 0].unsqueeze(1)  # [N,1]
        b1y1 = box1[:, 1].unsqueeze(1)
        b1x2 = box1[:, 2].unsqueeze(1)
        b1y2 = box1[:, 3].unsqueeze(1)
        b2x1 = box2[:, 0].unsqueeze(0)  # [1,N]
        b2y1 = box2[:, 1].unsqueeze(0)
        b2x2 = box2[:, 2].unsqueeze(0)
        b2y2 = box2[:, 3].unsqueeze(0)

        s = self.checkScript(iou, (b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2))
        self.assertAllFused(s.graph_for(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2),
                            except_for={'aten::size', 'prim::BroadcastSizes'})

        c = s(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2)
        torch.autograd.grad(c.sum(), [b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2])
        graph = backward_graph(s)
        self.assertAllFused(graph, except_for={'aten::size', 'prim::BroadcastSizes'})

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    @skipIfRocm
    @enable_cpu_fuser
    def test_fusion_reuse_multi_gpu(self):
        def fn(x, y):
            return x * y * x * y

        inputs_cpu = [
            torch.randn(4, 4, dtype=torch.float),
            torch.randn(4, 4, dtype=torch.float),
        ]
        inputs_cuda0 = [x.cuda(0) for x in inputs_cpu]
        inputs_cuda1 = [y.cuda(1) for y in inputs_cpu]

        # Should not crash; these should compile different kernels.
        ge = self.checkScript(fn, inputs_cpu)
        self.assertAllFused(ge.graph_for(*inputs_cpu))
        ge(*inputs_cuda0)
        ge(*inputs_cuda1)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    @skipIfRocm
    @enable_cpu_fuser
    def test_kernel_cache_multi_gpu(self):
        def not_fusible(x):
            return x

        def fn(x, y, z):
            x_out = x * x * x * x * x  # fusion: lambda x. x * x * x * x * x
            y_out = y * y * y * y * y
            z_out = z * z * z * z * z
            return not_fusible(x_out), not_fusible(y_out), not_fusible(z_out)

        inputs = [
            torch.randn(4, 4, dtype=torch.float),
            torch.randn(4, 4, dtype=torch.float, device='cuda:0'),
            torch.randn(4, 4, dtype=torch.float, device='cuda:1'),
        ]

        prev_cache_size = torch._C._jit_debug_fuser_num_cached_kernel_specs()

        # There are 3 FusionGroups. Because they have the same graph, they
        # should reuse the same KernelSpec in the KernelSpec cache.
        ge = self.checkScript(fn, inputs)
        self.assertGraphContainsExactly(
            ge.graph_for(*inputs), 'prim::FusionGroup', 3, True)
        new_cache_size = torch._C._jit_debug_fuser_num_cached_kernel_specs()
        # XXX: This assumes that the same kernel isn't already used by another test
        self.assertEqual(new_cache_size - prev_cache_size, 1)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    @skipIfRocm
    def test_nonzero_device_cuda(self):
        device = 'cuda:' + str(1)
        x = torch.tensor([0.4], dtype=torch.float, device=device)
        y = torch.tensor([0.7], dtype=torch.float, device=device)

        def doit(x, y):
            return torch.sigmoid(torch.tanh(x * (x + y) + x))

        ge = self.checkTrace(doit, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_cuda(self):
        inputs = get_lstm_inputs('cuda', training=True)
        module = self.checkScript(LSTMCellS, inputs)
        forward_graph = module.graph_for(*inputs)
        self.assertGraphContainsExactly(
            forward_graph, 'prim::FusionGroup', 1, consider_subgraphs=True)
        self.assertTrue(len(list(forward_graph.nodes())) == 2)
        # Everything is differentiable but TupleConstruct return
        FileCheck().check("DifferentiableGraph").check_next("TupleConstruct") \
            .check_next("return").run(str(forward_graph))

        hy, cy = module(*inputs)
        (hy + cy).sum().backward()
        backward = backward_graph(module)
        FileCheck().check("FusionGroup_0").check_next("FusionGroup_1") \
            .check_not("FusionGroup_2").run(str(backward))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_concat_cuda(self):
        inputs = get_lstm_inputs('cuda')
        ge = self.checkTrace(LSTMCellC, inputs)
        graph = ge.graph_for(*inputs)
        FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_gates_permutations_cuda(self):
        # lstm has gates = x.mm(w_ih.t()) + hx.mm(w_hh.t()) + b_ih + b_hh.
        # Test that any permutation of this will still result in one FusionGroup.
        choices = ['x.mm(w_ih.t())', 'hx.mm(w_hh.t())', 'b_ih', 'b_hh']
        template = dedent('''
        def cell(x, hx, cx, w_ih, w_hh, b_ih, b_hh):
            gates = {} + {} + {} + {}
            ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)
            return ingate * forgetgate * cellgate * outgate
        ''')
        for permutation in itertools.permutations(choices, len(choices)):
            code = template.format(*permutation)
            scope = {}
            exec(code, globals(), scope)
            cu = torch.jit.CompilationUnit(code)

            inputs = get_lstm_inputs('cuda', training=False)
            self.assertEqual(cu.cell(*inputs), scope['cell'](*inputs))
            forward_graph = cu.cell.graph_for(*inputs)
            self.assertGraphContainsExactly(forward_graph, 'prim::FusionGroup', 1)

    # TODO: Fuser doesn't work at all when inputs require grad. Fix that
    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_traced_cuda(self):
        inputs = get_lstm_inputs('cuda')
        ge = self.checkTrace(LSTMCellF, inputs)
        graph = ge.graph_for(*inputs)
        FileCheck().check_not("Chunk").check_not("aten::add").check_not("aten::sigmoid") \
            .check_not("aten::tanh").check("FusionGroup").check_next("TupleConstruct") \
            .check_next("return").check_not("FusionGroup_1").run(str(graph))

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @unittest.skip("Test is flaky, see https://github.com/pytorch/pytorch/issues/8746")
    @enable_cpu_fuser
    def test_lstm_traced_cpu(self):
        inputs = get_lstm_inputs('cpu')
        try:
            ge = self.checkTrace(LSTMCellF, inputs)
            graph = ge.graph_for(*inputs)
            FileCheck.check("FusionGroup").run(str(graph))
        except RuntimeError as e:
            if 'Failed to compile' in e.args[0]:
                warnings.warn('CPU fuser test has failed! This is not a hard failure, '
                              'because the kernels sometimes trigger bugs in compilers '
                              '(most notably GCC 7.2).')
                raise unittest.SkipTest('Failed to compile')
            else:
                raise

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_milstm_cuda(self):
        inputs = get_milstm_inputs('cuda', training=True)
        module = self.checkScript(MiLSTMCell, inputs)
        forward_graph = module.graph_for(*inputs)
        self.assertGraphContainsExactly(
            forward_graph, 'prim::FusionGroup', 1, consider_subgraphs=True)
        FileCheck().check("DifferentiableGraph").check_next("TupleConstruct") \
            .check_next("return").check("FusionGroup").run(str(forward_graph))
        hy, cy = module(*inputs)
        (hy + cy).sum().backward()

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_rand_cuda(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['d']

            def __init__(self):
                self.d = torch.device('cuda')

            @torch.jit.script_method
            def create(self, x):
                return x * x + x + torch.rand_like(x)

        x = torch.zeros([3, 4, 5], dtype=torch.float, device='cuda')
        m = M()
        out1 = m.create(x)
        out2 = m.create(x)
        self.assertNotEqual(out1, out2)
        self.assertTrue(torch.all(out1 >= 0))
        self.assertTrue(torch.all(out1 < 1))
        self.assertTrue(torch.all(out2 >= 0))
        self.assertTrue(torch.all(out2 < 1))
        self.assertAllFused(m.create.graph_for(x))

    @staticmethod
    def fn_test_relu(x, y):
        return F.relu(x + .5 * y)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_relu_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_relu, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_erf_cuda(self):
        def fn_test_erf(x):
            return F.relu(torch.erf(x) - torch.erfc(x))

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        ge = self.checkTrace(fn_test_erf, (x,))
        self.assertAllFused(ge.graph_for(x))
        x.requires_grad_(True)
        self.assertAllFused(ge.graph_for(x), except_for=("aten::size", "prim::BroadcastSizes"))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_rand_broadcast_cuda(self):
        def fn_test_rand(x, y):
            r = torch.rand_like(y)
            return r * x + x

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        script_f = torch.jit.script(fn_test_rand, (x, y))
        out = script_f(x, y)
        self.assertAllFused(script_f.graph_for(x, y))
        x.requires_grad_(True)
        out = script_f(x, y)
        self.assertAllFused(script_f.graph_for(x, y), except_for=("aten::size", "prim::BroadcastSizes"))
        # test that broadcasting random produces correct results
        x = torch.ones(4, 4, dtype=torch.float, device='cuda')
        y = torch.ones(4, dtype=torch.float, device='cuda')
        out = script_f(x, y)
        self.assertEqual(out[0], out[1])

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_scalar(self):
        def fn(x, y):
            return 2 * x + y

        x = torch.tensor(0.1, dtype=torch.float, device='cpu')
        y = torch.tensor(1, dtype=torch.float, device='cpu')
        ge = self.checkScript(fn, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_small_constant_cuda(self):
        def fn_test_small_constant(x, y):
            return (1e-8 * x + 5e-9 * y) * 1e8
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(fn_test_small_constant, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_tensor_scalar_ops_cuda(self):
        def should_fuse(x):
            z = 3.
            y = x + z
            return x * y

        # XXX: right now we only support fusing scalars if
        # they're constant (#9940)
        def should_not_fuse(x, z):
            y = x + int(z)
            return x * y

        inputs = [torch.randn(2, 2, dtype=torch.float, device='cuda')]
        ge = self.checkScript(should_fuse, inputs)
        self.assertAllFused(ge.graph_for(*inputs))

        inputs = [
            torch.randn(2, 2, dtype=torch.float, device='cuda'),
            torch.tensor(3., dtype=torch.float, device='cuda'),
        ]
        ge = self.checkScript(should_not_fuse, inputs)
        self.assertGraphContainsExactly(
            ge.graph_for(*inputs), 'prim::FusionGroup', 0, consider_subgraphs=True)

    @unittest.skipIf(IS_WINDOWS or IS_SANDCASTLE, "NYI: fuser support for Windows or Sandcastle")
    @enable_cpu_fuser
    def test_where_and_typing(self):
        def f(x, y):
            mask = x > y
            res = torch.where(mask, x, y)
            return mask, res

        script_f = torch.jit.script(f)

        x = torch.randn(4, 4, dtype=torch.double)
        y = torch.randn(4, 4, dtype=torch.double)

        result1, result2 = script_f(x, y)
        expected1, expected2 = f(x, y)
        self.assertEqual(result1, expected1)
        self.assertEqual(result2, expected2)
        self.assertAllFused(script_f.graph_for(x, y), except_for={'prim::TupleConstruct'})

    @unittest.skipIf(not IS_WINDOWS, "Test that the fuser is disabled on Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_windows_cuda(self):
        def scaleshift(x, scale, shift):
            return x * scale + shift

        inputs = [
            torch.randn(4, 4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
        ]

        ge = self.checkScript(scaleshift, inputs)
        self.assertGraphContainsExactly(
            ge.graph_for(*inputs), 'prim::FusionGroup', 0, consider_subgraphs=True)
