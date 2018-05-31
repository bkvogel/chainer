
import chainer


def static_schedule_func(*dec_args, **dec_kwargs):
    """Decorator to mark a function for inclusion in the forward schedule.

    This decorator is used to wrap a function `func` that is a forward-pass
    method of a subclass of FunctionNode. This will cause it to be added to
    the forward static schedule when the `static_graph` feature is
    enabled on a Chain that deeply calls it.

    The function to be wrapped should only return `None` because any
    return value
    will be ignored. Instead of returning its results, any result
    arrays must
    be supplied as input arguments and must have already have been
    initialized
    to the appropriate shapes and data types.

    todo: Consider allowing the wrapped function to return a
    list of
    arrays. If the function returns something other than None, the
    implementation
    will then check where these arrays are used later in the static schedule
    and update the needed references accordingly. This would be less
    efficient
    than writing the results in-place, but could also be more memory
    efficient
    and would allow us to more efficiently wrap existing functions
    that
    do not have built-in support for static graph optimizations.
    That is,
    the default way to automatically wrap/add support to existing
    functions
    would be to to let the wrapped static schedule function return
    a list
    of arrays as output and this implementation will update the used
    references later in the schedule.

    Usage:

    Typical usage is to allocate any required arrays in
    Python
    code in an instance of FunctionNode (See `LinearFunction` function
    for an example).
    Generally, this will involve first allocating storage for the
    results arrays
    in the `forward()` method of a sub-class of Function. Then, the
    FunctionNode.foward()
     method should call another
    method that is wrapped using this decorator. The
    decorated function will take all required input and output arrays as
    arguments and will not return anything (that is, `None` will be
    implicitly
    returned).

    Note that by following this usage convention, all input and output
    activations,
    along with any parameter arrays will have been statically allocated
    by the
    end of the first forward pass. Since the the forward-pass functions
    that
    are used inside the forward static schedule (that is, the functions
    that
    use this decorator) do not allocate any results arrays, this results
    in code that
    looks like 'define-by-run' to the user, and which can be debugged
    during
    the first iteration, but then becomes static in terms of memory
    allocations and
    scheduling starting from the second iteration. Thus, we get the
    benefit of
    both ease of use and optimized performance.

    It is important that all of the required computations that occur
    during the
    second  and later forward passes must be contained inside the
    functions
    that use this decorator. That is, any other code (that is not w
    rapped inside this
    decorator) in the various FunctionNode and Link instances can be
    viewed as
    setup code that only checks types, allocates result arrays, initializes
    parameters etc., but does not perform any computations that must
    be repeated after the first forward pass.

    The reason for this is that after the first iteration (that is,
    starting
    from the second forward pass), when the chain's `__call__()` is
    called,
    the forward static schedule will be invoked and it will only call the
    functions that were wrapped with this decorator. Note that this can
    potentially
    lead to difficult to find bugs if one forgets to decorate a required
    function,
    since the corresponding computations would no longer execute after the
    first iteration. As a general rule, any code that is intended to
    exectue on
    each iteration should be placed into a function that uses this decorator.

    Args:
        delay_call (bool): Optional keyword argument. If True, don't
        call the wrapped
        function during the define by run pass, but only add it to the
        static schedule.
        Default value is False.

        func: A forward-pass method of a sub-class of FunctionNode that will
        be inserted
            into the static forward schedule when `static_graph` is enabled.
            The function
            must not return anything because any return values will be
            ignored.

    Returns: The wrapped function.

    """
    delay_call = False
    func_name = None
    zero_args = False
    if len(dec_args) == 1 and not dec_kwargs and callable(dec_args[0]):
        callable_arg = dec_args[0]
        zero_args = True
    elif dec_kwargs:
        if 'delay_call' in dec_kwargs:
            delay_call = dec_kwargs['delay_call']
        if 'func_name' in dec_kwargs:
            func_name = dec_kwargs['func_name']

    def wrap(func):
        def wrapped_func(*args, **kwargs):
            # Save arguments, function, and results pointers/references
            # to the schedule list:
            if not delay_call:
                ret = func(*args, **kwargs)
                if ret is not None:
                    # todo: We can allow it to return tuple of
                    # arrays in the future.
                    raise RuntimeError("This function is not supposed to "
                                       "return anything: ", func)

            # If trace mode is on, add to schedule.
            schedule_function = chainer.config.schedule_func
            if schedule_function is not None:
                schedule_function.append_function(func, args, kwargs,
                                                  func_name=func_name)
                # Add the schedule function as an attribute of the
                # FunctionNode instance
                # that contains the wrapped function as a method
                # This attribute will be needed by the corresponding
                # @static_backward
                # function.
                instance = args[0]
                instance._supports_static_optimizations = True
                instance.schedule_func = schedule_function

        return wrapped_func

    if zero_args:
        return wrap(callable_arg)
    else:
        return wrap


def static_forward_optimizations(func, in_data, outputs):
    """Perform checks needed for creation of a static schedule.

    Check if `func` supports static graph optimizations. If not, try
    to automatically wrap it to be compatible.

    This function should be called from the ``FunctionNode`` apply() method
    just after func.forward() is called.

    Args:
        func (instance of FunctionNode):
        in_data (tuple of ndarray): input arrays to func
        outputs (tuple of ndarray): outputs of func.

    """

    schedule_function = chainer.config.schedule_func
    if schedule_function is not None:
        if not func._supports_static_optimizations:
            if schedule_function.verbosity_level >= 2:
                print("Adding automatic static graph support to "
                      "function: ", func)

            # func does not already support static optimizations, so wrap it.
            @static_schedule_func(delay_call=True, func_name=str(func))
            def generic_static_forward(func, in_data, out_data,
                                       is_generic_static_forward):
                """Auto-wrap the supplied function.

                in_arrs: list of input arrays
                out_arrs: list of output arrays
                func: compatible with out_arrs = func(in_arrs)
                """
                in_data = tuple(in_data)
                temp_out_data = func.forward(in_data)
                # todo: Note that instead of copying the data from
                # temp_out_data into the static arrays in out_data,
                # we could simply return temp_out_data and then
                # track where it is used again later (by tracking)
                # its reference id(temp_out_data). This would
                # reduce copy overhead and save memory but
                # would also introduce additional Python code
                # overhead in the static schedule.
                for ind, static_ar in enumerate(out_data):
                    static_ar[...] = temp_out_data[ind]

            generic_static_forward(func, list(in_data), list(outputs),
                                   is_generic_static_forward=True)