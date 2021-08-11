cdef class Span:
    cdef unsigned long long c_trace_id
    cdef unsigned long long c_span_id
    cdef unsigned long long c_parent_id
    cdef char* c_service
    cdef char* c_resource
    cdef char* c_name
    cdef char* c_span_type
    cdef bint c_error
    cdef unsigned long long c_start_ns
    cdef unsigned long long c_duration_ns
