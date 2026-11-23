/* Asterisk 20.22.0 main/app.c; GPL-2.0; commit 7c225dc47bb43bf9862b2b147ed565b7b6113fed.
 * Unmodified function used to exercise the actual argument grammar. */
#include <string.h>
unsigned int __ast_app_separate_args(char *buf, char delim, int remove_chars, char **array, int arraylen)
{
	int argc;
	char *scan, *wasdelim = NULL;
	int paren = 0, quote = 0, bracket = 0;

	if (!array || !arraylen) {
		return 0;
	}

	memset(array, 0, arraylen * sizeof(*array));

	if (!buf) {
		return 0;
	}

	scan = buf;

	for (argc = 0; *scan && (argc < arraylen - 1); argc++) {
		array[argc] = scan;
		for (; *scan; scan++) {
			if (*scan == '(') {
				paren++;
			} else if (*scan == ')') {
				if (paren) {
					paren--;
				}
			} else if (*scan == '[') {
				bracket++;
			} else if (*scan == ']') {
				if (bracket) {
					bracket--;
				}
			} else if (*scan == '"' && delim != '"') {
				quote = quote ? 0 : 1;
				if (remove_chars) {
					/* Remove quote character from argument */
					memmove(scan, scan + 1, strlen(scan));
					scan--;
				}
			} else if (*scan == '\\') {
				if (remove_chars) {
					/* Literal character, don't parse */
					memmove(scan, scan + 1, strlen(scan));
				} else {
					scan++;
				}
			} else if ((*scan == delim) && !paren && !quote && !bracket) {
				wasdelim = scan;
				*scan++ = '\0';
				break;
			}
		}
	}

	/* If the last character in the original string was the delimiter, then
	 * there is one additional argument. */
	if (*scan || (scan > buf && (scan - 1) == wasdelim)) {
		array[argc++] = scan;
	}

	return argc;
}
