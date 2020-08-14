# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

"""
All custom model containers are listed here.
"""

from abc import ABC
import numpy as np
from onnxconverter_common.container import CommonSklearnModelContainer
import torch

from .operator_converters import constants
from ._utils import onnx_runtime_installed


class CommonONNXModelContainer(CommonSklearnModelContainer):
    """
    Common container for input ONNX operators.
    """

    def __init__(self, onnx_model):
        super(CommonONNXModelContainer, self).__init__(onnx_model)


def _get_device(model):
    """
    Convenient function used to get the runtime device for the model.
    """
    device = None
    if len(list(model.parameters())) > 0:
        device = next(model.parameters()).device  # Assuming we are using a single device for all parameters

    return device


class PyTorchBackendModel(torch.nn.Module):
    """
    Hummingbird model representing a converted pipeline.
    """

    def __init__(self, input_names, output_names, operator_map, operators, extra_config):
        """
        Args:
            input_names: The names of the input `onnxconverter_common.topology.Variable`s for this model
            output_names: The names of the output `onnxconverter_common.topology.Variable`s generated by this model
            operator_map: A dictionary of operator aliases and related PyTorch implementations
            operators: The list of operators (in a topological order) that will be executed by the model (in order)
            extra_config: Some additional custom configuration parameter
        """
        super(PyTorchBackendModel, self).__init__()
        self._input_names = input_names
        self._output_names = output_names
        self._operator_map = torch.nn.ModuleDict(operator_map)
        self._operators = operators

    def forward(self, *inputs):
        with torch.no_grad():
            inputs = [*inputs]
            variable_map = {}
            device = _get_device(self)

            # Maps data inputs to the expected variables.
            for i, input_name in enumerate(self._input_names):
                if type(inputs[i]) is np.ndarray:
                    inputs[i] = torch.from_numpy(inputs[i]).float()
                elif type(inputs[i]) is not torch.Tensor:
                    raise RuntimeError("Inputer tensor {} of not supported type {}".format(input_name, type(inputs[i])))
                if device != "cpu":
                    inputs[i] = inputs[i].to(device)
                variable_map[input_name] = inputs[i]

            # Evaluate all the operators in the topology by properly wiring inputs \ outputs
            for operator in self._operators:
                pytorch_op = self._operator_map[operator.full_name]
                pytorch_outputs = pytorch_op(*(variable_map[input] for input in operator.input_full_names))

                if len(operator.output_full_names) == 1:
                    variable_map[operator.output_full_names[0]] = pytorch_outputs
                else:
                    for i, output in enumerate(operator.output_full_names):
                        variable_map[output] = pytorch_outputs[i]

            # Prepare and return the output.
            if len(self._output_names) == 1:
                return variable_map[self._output_names[0]]
            else:
                return list(variable_map[output_name] for output_name in self._output_names)


class PyTorchTorchscriptSklearnContainer(ABC):
    """
    Base container for PyTorch and TorchScript models.
    The container allows to mirror the Sklearn API.
    """

    def __init__(self, model, extra_config={}):
        """
        Args:
            model: A pytorch or torchscript model
            extra_config: Some additional configuration parameter
        """
        self._model = model
        self._extra_config = extra_config

    @property
    def model(self):
        return self._model

    def to(self, device):
        """
        Set the target device for the model.

        Args:
            device: The target device.
        """
        self.model.to(device)


# PyTorch containers.
class PyTorchSklearnContainerTransformer(PyTorchTorchscriptSklearnContainer):
    """
    Container mirroring Sklearn transformers API.
    """

    def transform(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On data transformers it returns transformed output data
        """
        return self.model.forward(*inputs).cpu().numpy()


class PyTorchSklearnContainerRegression(PyTorchTorchscriptSklearnContainer):
    """
    Container mirroring Sklearn regressors API.
    """

    def __init__(self, model, extra_config={}, is_regression=True, is_anomaly_detection=False, **kwargs):
        super(PyTorchSklearnContainerRegression, self).__init__(model, extra_config)

        assert not (is_regression and is_anomaly_detection)

        self._is_regression = is_regression
        self._is_anomaly_detection = is_anomaly_detection

    def predict(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On regression returns the predicted values.
        On classification tasks returns the predicted class labels for the input data.
        On anomaly detection (e.g. isolation forest) returns the predicted classes (-1 or 1).
        """
        if self._is_regression:
            return self.model.forward(*inputs).cpu().numpy().flatten()
        elif self._is_anomaly_detection:
            return self.model.forward(*inputs)[0].cpu().numpy().flatten()
        else:
            return self.model.forward(*inputs)[0].cpu().numpy()


class PyTorchSklearnContainerClassification(PyTorchSklearnContainerRegression):
    """
    Container mirroring Sklearn classifiers API.
    """

    def __init__(self, model, extra_config={}):
        super(PyTorchSklearnContainerClassification, self).__init__(model, extra_config, is_regression=False)

    def predict_proba(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On classification tasks returns the probability estimates.
        """
        return self.model.forward(*inputs)[1].cpu().numpy()


class PyTorchSklearnContainerAnomalyDetection(PyTorchSklearnContainerRegression):
    """
    Container mirroring Sklearn anomaly detection API.
    """

    def __init__(self, model, extra_config={}):
        super(PyTorchSklearnContainerAnomalyDetection, self).__init__(
            model, extra_config, is_regression=False, is_anomaly_detection=True
        )

    def decision_function(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision function scores.
        """
        return self.model.forward(*inputs)[1].cpu().numpy().flatten()

    def score_samples(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision_function score plus offset_
        """
        return self.decision_function(*inputs) + self._extra_config[constants.OFFSET]


# TorchScript containers.
def _torchscript_wrapper(device, function, *inputs):
    """
    This function contains the code to enable predictions over torchscript models.
    It used to wrap pytorch container functions.
    """
    inputs = [*inputs]

    with torch.no_grad():
        # Maps data inputs to the expected type and device.
        for i in range(len(inputs)):
            if type(inputs[i]) is np.ndarray:
                inputs[i] = torch.from_numpy(inputs[i]).float()
            elif type(inputs[i]) is not torch.Tensor:
                raise RuntimeError("Inputer tensor {} of not supported type {}".format(i, type(inputs[i])))
            if device is not None:
                inputs[i] = inputs[i].to(device)
        return function(*inputs)


class TorchScriptSklearnContainerTransformer(PyTorchSklearnContainerTransformer):
    """
    Container mirroring Sklearn transformers API.
    """

    def transform(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerTransformer, self).transform

        return _torchscript_wrapper(device, f, *inputs)


class TorchScriptSklearnContainerRegression(PyTorchSklearnContainerRegression):
    """
    Container mirroring Sklearn regressors API.
    """

    def predict(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerRegression, self).predict

        return _torchscript_wrapper(device, f, *inputs)


class TorchScriptSklearnContainerClassification(PyTorchSklearnContainerClassification):
    """
    Container mirroring Sklearn classifiers API.
    """

    def predict_proba(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerClassification, self).predict_proba

        return _torchscript_wrapper(device, f, *inputs)


class TorchScriptSklearnContainerAnomalyDetection(PyTorchSklearnContainerAnomalyDetection):
    """
    Container mirroring Sklearn anomaly detection API.
    """

    def predict(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerAnomalyDetection, self).predict

        return _torchscript_wrapper(device, f, *inputs)

    def decision_function(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerAnomalyDetection, self).decision_function

        return _torchscript_wrapper(device, f, *inputs)

    def score_samples(self, *inputs):
        device = _get_device(self.model)
        f = super(TorchScriptSklearnContainerAnomalyDetection, self).score_samples

        return _torchscript_wrapper(device, f, *inputs)


# ONNX containers.
class ONNXSklearnContainer(ABC):
    """
    Base container for ONNX models.
    The container allows to mirror the Sklearn API.
    """

    def __init__(self, model, extra_config={}):
        """
        Args:
            model: A ONNX model
            extra_config: Some additional configuration parameter
        """
        if onnx_runtime_installed():
            import onnxruntime as ort

        self.model = model
        self._extra_config = extra_config

        self.session = ort.InferenceSession(self.model.SerializeToString())
        self._output_names = [self.session.get_outputs()[i].name for i in range(len(self.session.get_outputs()))]
        self.input_names = [input.name for input in self.session.get_inputs()]

    def _get_named_inputs(self, *inputs):
        assert len(inputs) == len(self.input_names)

        named_inputs = {}

        for i in range(len(inputs)):
            named_inputs[self.input_names[i]] = inputs[i]

        return named_inputs


class ONNXSklearnContainerTransformer(ONNXSklearnContainer):
    """
    Container mirroring Sklearn transformers API.
    """

    def __init__(self, model, extra_config={}):
        super(ONNXSklearnContainerTransformer, self).__init__(model, extra_config)

        assert len(self._output_names) == 1

    def transform(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On data transformers it returns transformed output data
        """
        named_inputs = self._get_named_inputs(*inputs)

        return self.session.run(self._output_names, named_inputs)


class ONNXSklearnContainerRegression(ONNXSklearnContainer):
    """
    Container mirroring Sklearn regressors API.
    """

    def __init__(self, model, extra_config={}, is_regression=True, is_anomaly_detection=False, **kwargs):
        super(ONNXSklearnContainerRegression, self).__init__(model, extra_config)

        assert not (is_regression and is_anomaly_detection)
        if is_regression:
            assert len(self._output_names) == 1

        self._is_regression = is_regression
        self._is_anomaly_detection = is_anomaly_detection

    def predict(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On regression returns the predicted values.
        On classification tasks returns the predicted class labels for the input data.
        On anomaly detection (e.g. isolation forest) returns the predicted classes (-1 or 1).
        """
        named_inputs = self._get_named_inputs(*inputs)

        if self._is_regression:
            return self.session.run(self._output_names, named_inputs)
        else:
            return self.session.run([self._output_names[0]], named_inputs)[0]


class ONNXSklearnContainerClassification(ONNXSklearnContainerRegression):
    """
    Container mirroring Sklearn classifiers API.
    """

    def __init__(self, model, extra_config={}):
        super(ONNXSklearnContainerClassification, self).__init__(model, extra_config, is_regression=False)

        assert len(self._output_names) == 2

    def predict_proba(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On classification tasks returns the probability estimates.
        """
        named_inputs = self._get_named_inputs(*inputs)

        return self.session.run([self._output_names[1]], named_inputs)[0]


class ONNXSklearnContainerAnomalyDetection(ONNXSklearnContainerRegression):
    """
    Container mirroring Sklearn anomaly detection API.
    """

    def __init__(self, model, extra_config={}):
        super(ONNXSklearnContainerAnomalyDetection, self).__init__(
            model, extra_config, is_regression=False, is_anomaly_detection=True
        )

        assert len(self._output_names) == 2

    def decision_function(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision function scores.
        """
        named_inputs = self._get_named_inputs(*inputs)

        return self.session.run([self._output_names[1]], named_inputs)

    def score_samples(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision_function score plus offset_
        """
        return self.decision_function(*inputs) + self._extra_config[constants.OFFSET]
