/* Asterisk 20.22.0 main/utils.c; GPL-2.0; commit 7c225dc47bb43bf9862b2b147ed565b7b6113fed. */
#include <string.h>
static char base64[64];
static char base64url[64];
static char b2a_url[256];
static char b2a[256];
int ast_base64decode(unsigned char *dst, const char *src, int max)
{
	int cnt = 0;
	unsigned int byte = 0;
	unsigned int bits = 0;
	while(*src && *src != '=' && (cnt < max)) {
		/* Shift in 6 bits of input */
		byte <<= 6;
		byte |= (b2a[(int)(*src)]) & 0x3f;
		bits += 6;
		src++;
		/* If we have at least 8 bits left over, take that character
		   off the top */
		if (bits >= 8)  {
			bits -= 8;
			*dst = (byte >> bits) & 0xff;
			dst++;
			cnt++;
		}
	}
	/* Don't worry about left over bits, they're extra anyway */
	return cnt;
}
static void base64_init(void)
{
	int x;
	memset(b2a, -1, sizeof(b2a));
	memset(b2a_url, -1, sizeof(b2a_url));
	/* Initialize base-64 Conversion table */
	for (x = 0; x < 26; x++) {
		/* A-Z */
		base64[x] = 'A' + x;
		base64url[x] = 'A' + x;
		b2a['A' + x] = x;
		b2a_url['A' + x] = x;
		/* a-z */
		base64[x + 26] = 'a' + x;
		base64url[x + 26] = 'a' + x;
		b2a['a' + x] = x + 26;
		b2a_url['a' + x] = x + 26;
		/* 0-9 */
		if (x < 10) {
			base64[x + 52] = '0' + x;
			base64url[x + 52] = '0' + x;
			b2a['0' + x] = x + 52;
			b2a_url['0' + x] = x + 52;
		}
	}
	base64[62] = '+';
	base64[63] = '/';
	base64url[62] = '-';
	base64url[63] = '_';
	b2a[(int)'+'] = 62;
	b2a[(int)'/'] = 63;
	b2a_url[(int)'-'] = 62;
	b2a_url[(int)'_'] = 63;
}
