/** The chat route, spelled once. Two things key off it on the phone -- the
 *  single-bar header variant and the shell nav drawer's swipe gate -- and a
 *  drift between two spellings is exactly how "two drawers for one gesture"
 *  would come back. Outside the shell so a status item's go can read it too. */
export const isChatRoute = (pathname: string) => pathname === '/chat' || pathname.startsWith('/chat/') || pathname === '/'
