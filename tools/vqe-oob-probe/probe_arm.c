/* Compatibility entry point. See capacity_probe.c for the checked implementation.
 * ASan does NOT guarantee detection at free() of writes by uninstrumented code.
 */
#include "capacity_probe.c"
