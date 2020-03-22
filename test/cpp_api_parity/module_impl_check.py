# The purpose of this test is to check that we have implementation parity between
# a Python `torch.nn` module and its corresponding C++ `torch::nn` module. Concretely,
# this test does the following:
#
# 1. Get a test params dict from common_nn.py, run forward and backward on the
# Python module created using the test params.
#
# 2. Serialize the Python module's parameters / buffers and its forward input
# arguments, deserialize them in C++ and load them into the C++ module.
#
# 3. Run the same forward and backward passes on the C++ module, and serialize
# the C++ module's forward output and backward gradients.
#
# 4. Compare Python/C++ module's forward output and backward gradients. If they
# are the same, then we have implementation parity between Python/C++ module.

import tempfile
import shutil
from string import Template
import unittest
import types

import torch
import torch.testing._internal.common_nn as common_nn
from torch.testing._internal.common_cuda import TEST_CUDA
from cpp_api_parity.utils import TorchNNModuleTestParams, CppArg, TORCH_NN_COMMON_TEST_HARNESS, \
  compile_cpp_code_inline, convert_to_list, set_python_tensors_requires_grad, move_python_tensors_to_device, \
  has_test, add_test, set_cpp_tensors_requires_grad, move_cpp_tensors_to_device, is_criterion_test, \
  compute_cpp_args_construction_stmts_and_forward_arg_symbols, serialize_arg_dict_as_script_module, \
  compute_arg_dict, skip_test_fn_if_needed
from cpp_api_parity import torch_nn_modules

# NN tests use double as the default dtype
torch.set_default_dtype(torch.double)

# Expected substitutions:
#
# ${module_variant_name}
# ${module_qualified_name}
# ${cpp_tmp_folder}
# ${cpp_args_construction_stmts}
# ${cpp_constructor_args}
# ${device}
# ${cpp_forward_args_symbols}
TORCH_NN_MODULE_TEST_FORWARD_BACKWARD = Template("""
void ${module_variant_name}_test_forward_backward() {
  pybind11::gil_scoped_release no_gil;

  // Declare arguments
  auto arg_dict = load_dict_from_file("${cpp_tmp_folder}/${module_variant_name}_arg_dict.pt");
  ${cpp_args_construction_stmts};

  // Construct module and load params/buffers from Python module
  ${module_qualified_name} module${cpp_constructor_args};
  torch::load(module, "${cpp_tmp_folder}/${module_variant_name}_module.pt");
  module->to(std::string("${device}"));

  // Some modules (such as `RReLU`) create random tensors in their forward pass.
  // To make sure the random tensors created are the same in Python/C++, we need
  // to set the RNG seed manually.
  torch::manual_seed(0);

  // Forward pass
  auto cpp_output = module(${cpp_forward_args_symbols});

  // Save the output into a file to be compared in Python later
  write_ivalue_to_file(
    torch::IValue(cpp_output),
    "${cpp_tmp_folder}/${module_variant_name}_forward_output.pt");

  // Backward pass
  cpp_output.sum().backward();

  // Put all gradients into a c10::Dict, save it into a file to be compared in Python later
  c10::Dict<std::string, torch::Tensor> grad_dict;
  for (const auto& param : module->named_parameters()) {
    torch::Tensor grad = param.value().grad();
    if (grad.is_sparse()) {
      grad = grad.to_dense();
    }
    grad_dict.insert(param.key() + "_grad", grad);
  }

  write_ivalue_to_file(
    torch::IValue(grad_dict),
    "${cpp_tmp_folder}/${module_variant_name}_backward_grad_dict.pt");
}
""")

def run_python_forward_backward(unit_test_class, test_params):
  device = test_params.device
  module = test_params.test_instance.constructor(*test_params.test_instance.constructor_args).to(device)

  inputs = set_python_tensors_requires_grad([arg_value for _, arg_value in test_params.arg_dict['input']])
  inputs = inputs + [arg_value for _, arg_value in test_params.arg_dict['target']]
  inputs = inputs + [arg_value for _, arg_value in test_params.arg_dict['extra_args']]
  inputs = move_python_tensors_to_device(inputs, device)

  # Some modules (such as `RReLU`) create random tensors in their forward pass.
  # To make sure the random tensors created are the same in Python/C++, we need
  # to set the RNG seed manually.
  torch.manual_seed(0)

  # Forward pass
  python_output = module(*inputs)

  # NOTE: This is a workaround to allow any module to be traced.
  # We can do this because we are only interested in transferring
  # the Python module's parameters and buffers to the C++ module.
  module.forward = types.MethodType(lambda self, input: input, module)
  script_module = torch.jit.trace(module, torch.tensor(0))

  # Backward pass
  python_output.sum().backward()

  # Put all gradients into a dict, to be compared later
  python_grad_dict = {}
  for name, param in module.named_parameters():
    grad = param.grad;
    if grad.is_sparse:
      grad = grad.to_dense()
    python_grad_dict[name + "_grad"] = grad

  return script_module, python_output, python_grad_dict

def test_forward_backward(unit_test_class, test_params):
  module_variant_name = test_params.module_variant_name

  # Run forward and backward on Python module
  script_module, python_output, python_grad_dict = run_python_forward_backward(unit_test_class, test_params)

  # Save Python module and arguments to be used from C++ function
  script_module.save("{}/{}_module.pt".format(test_params.cpp_tmp_folder, module_variant_name))
  serialize_arg_dict_as_script_module(test_params.arg_dict).save("{}/{}_arg_dict.pt".format(test_params.cpp_tmp_folder, module_variant_name))

  cpp_test_name = '{}_{}'.format(test_params.module_variant_name, 'test_forward_backward')
  cpp_test_fn = getattr(unit_test_class.module_impl_check_cpp_module, cpp_test_name)

  def run_cpp_test_fn_and_check_output():
    cpp_test_fn()
    cpp_output = torch.load("{}/{}_forward_output.pt".format(test_params.cpp_tmp_folder, module_variant_name))
    cpp_grad_dict = torch.load("{}/{}_backward_grad_dict.pt".format(test_params.cpp_tmp_folder, module_variant_name))

    def generate_error_msg(name, cpp_value, python_value):
      return "Parity test failed: {} in C++ has value: {}, which does not match the corresponding value in Python: {}".format(
        name, cpp_value, python_value)

    # Check that forward outputs are equal
    unit_test_class.assertTrue(
      torch.allclose(python_output, cpp_output),
      generate_error_msg("forward output", cpp_output, python_output))

    # Check that module parameter gradients are equal after backward pass
    unit_test_class.assertEqual(
      len(python_grad_dict), len(cpp_grad_dict),
      generate_error_msg("# of parameters", len(cpp_grad_dict), len(python_grad_dict)))
    for key in python_grad_dict:
      unit_test_class.assertTrue(
        key in cpp_grad_dict,
        generate_error_msg("\"Does module have a parameter named `{}`?\"".format(key[:-5]), False, True))
      unit_test_class.assertTrue(
        torch.allclose(python_grad_dict[key], cpp_grad_dict[key]),
        generate_error_msg("gradient of `{}`".format(key[:-5]), cpp_grad_dict[key], python_grad_dict[key]))

  if not test_params.has_parity:
    with unit_test_class.assertRaisesRegex(AssertionError, "Parity test failed"):
      run_cpp_test_fn_and_check_output()
  else:
    run_cpp_test_fn_and_check_output()

  # Remove temporary folder that stores C++ outputs
  shutil.rmtree(test_params.cpp_tmp_folder)

def test_torch_nn_module_variant(unit_test_class, test_params):
  test_forward_backward(unit_test_class, test_params)

def compute_module_name(test_params_dict):
    fullname = test_params_dict.get('fullname', None)
    if fullname:
        # NOTE: This doesn't work for some of the `wrap_functional` module tests such as "interpolate_nearest_1d",
        # because in that case the module `interpolate` is not in `torch.nn` but rather in `torch.nn.functional`.
        # We will fix this when we have parity tests for `torch.nn.functional` modules.
        module_name = fullname.split('_')[0]
    else:
        module_name = test_params_dict.get('module_name')
    return module_name

def process_test_params_for_module(test_params_dict, device, test_instance_class):
  module_name = compute_module_name(test_params_dict)
  test_params_dict['constructor'] = test_params_dict.get('constructor', getattr(torch.nn, module_name))
  test_instance = test_instance_class(**test_params_dict)
  assert test_instance.get_name().startswith('test_')
  module_variant_name = test_instance.get_name()[5:] + (('_' + device) if device != 'cpu' else '')

  if 'constructor_args' in test_params_dict:
    assert 'cpp_constructor_args' in test_params_dict, \
      "If `constructor_args` is present in test params dict, `cpp_constructor_args` must be present: {}".format(
        test_params_dict)

  return TorchNNModuleTestParams(
    module_name=module_name,
    module_variant_name=module_variant_name,
    test_instance=test_instance,
    cpp_constructor_args=test_params_dict.get('cpp_constructor_args', ''),
    arg_dict=compute_arg_dict(test_params_dict, test_instance),
    has_parity=test_params_dict.get('has_parity', True),
    device=device,
    cpp_tmp_folder=tempfile.mkdtemp(),
  )

torch_nn_test_params_map = {}

def add_torch_nn_module_impl_parity_tests(parity_table, unit_test_class, test_params_dicts, test_instance_class, devices):
  for test_params_dict in test_params_dicts:
    # Skip all `torch.nn.functional` tests, since they are handled by another test suite.
    if 'FunctionalModule' in str(test_params_dict.get('constructor', '')):
      continue

    module_name = compute_module_name(test_params_dict)

    assert hasattr(torch.nn, module_name), \
      "`torch.nn` doesn't have module `{}`. ".format(module_name) + \
      "If you are adding a new test, please set `fullname` using format `ModuleName_desc`, " + \
      "or set `module_name` using format `ModuleName`. " + \
      "(Discovered while processing {}.)".format(test_params_dict)

    module_full_name = 'torch::nn::' + module_name

    assert module_full_name in parity_table['torch::nn'], \
      "Please add `{}` entry to `torch::nn` section of `test/cpp_api_parity/parity-tracker.md`. (Discovered while processing {}.)".format(
        module_full_name, test_params_dict)

    has_impl_parity, _ = parity_table['torch::nn'][module_full_name]

    for device in devices:
      test_params = process_test_params_for_module(
        test_params_dict=test_params_dict,
        device=device,
        test_instance_class=test_instance_class,
      )
      test_name = 'test_torch_nn_{}'.format(test_params.module_variant_name)
      torch_nn_test_params_map[test_name] = test_params

      def test_fn(self):
        test_torch_nn_module_variant(unit_test_class=self, test_params=torch_nn_test_params_map[self._testMethodName])

      test_fn = skip_test_fn_if_needed(
        test_fn=test_fn,
        test_params_dict=test_params_dict,
        test_cuda=TEST_CUDA,
        has_impl_parity=has_impl_parity,
        device=device)
      add_test(unit_test_class, test_name, test_fn)

def add_tests(unit_test_class, test_params_dicts, test_instance_class, parity_table, devices):
  add_torch_nn_module_impl_parity_tests(
    parity_table=parity_table,
    unit_test_class=unit_test_class,
    test_params_dicts=test_params_dicts,
    test_instance_class=test_instance_class,
    devices=devices)

def generate_test_cpp_sources(test_params, template):
  device = test_params.device

  cpp_constructor_args = test_params.cpp_constructor_args
  if cpp_constructor_args != '':
    cpp_constructor_args = '({})'.format(cpp_constructor_args)

  cpp_args_construction_stmts, cpp_forward_args_symbols = compute_cpp_args_construction_stmts_and_forward_arg_symbols(test_params)

  test_cpp_sources = template.substitute(
    module_variant_name=test_params.module_variant_name,
    module_qualified_name='torch::nn::{}'.format(test_params.module_name),
    cpp_args_construction_stmts=";\n  ".join(cpp_args_construction_stmts),
    cpp_constructor_args=cpp_constructor_args,
    cpp_forward_args_symbols=", ".join(cpp_forward_args_symbols),
    cpp_tmp_folder=test_params.cpp_tmp_folder,
    device=device,
  )
  return test_cpp_sources

def build_cpp_tests(unit_test_class, print_cpp_source=False):
  if len(torch_nn_test_params_map) > 0:
    cpp_sources = TORCH_NN_COMMON_TEST_HARNESS
    functions = []
    modules_added_metadata_cpp_sources = set()
    for test_name, test_params in torch_nn_test_params_map.items():
      if not test_params.module_name in modules_added_metadata_cpp_sources:
        cpp_sources += torch_nn_modules.module_metadata_map.get(test_params.module_name, torch_nn_modules.TorchNNModuleMetadata()).cpp_sources
        modules_added_metadata_cpp_sources.add(test_params.module_name)
      cpp_sources += generate_test_cpp_sources(test_params=test_params, template=TORCH_NN_MODULE_TEST_FORWARD_BACKWARD)
      functions.append('{}_{}'.format(test_params.module_variant_name, 'test_forward_backward'))
    if print_cpp_source:
      print(cpp_sources)

    cpp_module = compile_cpp_code_inline(
      name='module_impl_check',
      cpp_sources=cpp_sources,
      functions=functions)
    unit_test_class.module_impl_check_cpp_module = cpp_module
