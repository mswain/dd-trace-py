# cython: c_string_type=unicode, c_string_encoding=utf8

from cpython cimport PyBytes_Check, PyUnicode_Check, PyUnicode_AsEncodedString
from cpython.bytearray cimport PyByteArray_Check
from libc.stdlib cimport free


cdef inline char* object_as_cstr(object value):
    if value is None:
        return NULL
    elif PyBytes_Check(value) or PyByteArray_Check(value):
        return value
    elif PyUnicode_Check(value):
        IF PY_MAJOR_VERSION >= 3:
            return value
        ELSE:
            encoded = PyUnicode_AsEncodedString(value, "utf-8", NULL)
            return encoded
    else:
        str_value = <str> value
        return str_value


cdef class Span:

    @property
    def trace_id(self):
        return self.c_trace_id

    @trace_id.setter
    def trace_id(self, object value):
        self.c_trace_id = value

    @property
    def span_id(self):
        return self.c_span_id

    @span_id.setter
    def span_id(self, object value):
        self.c_span_id = value

    @property
    def parent_id(self):
        return self.c_parent_id

    @parent_id.setter
    def parent_id(self, object value):
        if value is None:
            self.c_parent_id = 0
        else:
            self.c_parent_id = value

    @property
    def service(self):
        if self.c_service == NULL:
            return None
        return self.c_service

    @service.setter
    def service(self, object value):
        self.c_service = object_as_cstr(value)

    @property
    def resource(self):
        if self.c_resource == NULL:
            return None
        return self.c_resource

    @resource.setter
    def resource(self, object value):
        self.c_resource = object_as_cstr(value)

    @property
    def name(self):
        if self.c_name == NULL:
            return None
        return self.c_name

    @name.setter
    def name(self, object value):
        self.c_name = object_as_cstr(value)

    # TODO: Make this be `def span_type`
    @property
    def _span_type(self):
        if self.c_span_type == NULL:
            return None
        return self.c_span_type

    @_span_type.setter
    def _span_type(self, object value):
        self.c_span_type = object_as_cstr(value)

    @property
    def error(self):
        return self.c_error == 1

    @error.setter
    def error(self, object value):
        self.c_error = bool(value)

    @property
    def start_ns(self):
        return self.c_start_ns

    @start_ns.setter
    def start_ns(self, object value):
        self.c_start_ns = value

    @property
    def duration_ns(self):
        return self.c_duration_ns

    @duration_ns.setter
    def duration_ns(self, object value):
        if value is None:
            self.c_duration_ns = 0
        else:
            self.c_duration_ns = value
