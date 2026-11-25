<?php
/*
 * Network Galaxy block-list sync for pfSense. Runs from cron every minute as root.
 *
 * Pulls the addresses you blocked in Network Galaxy, checks every line again here, and loads what
 * passes into the pfBlockerNG table pfB_NetGalaxy_v4 (pfBlockerNG's "NetGalaxy" list, Deny Both,
 * reads a copy, /var/db/pfblockerng/netgalaxy.txt, on its hourly update, so the table survives filter reloads and reboots).
 * On each fetch it reports the hash of the list it has loaded, so the app knows what is in force.
 *
 * It also reports who is connected to the VPN: the OpenVPN server's own client list (user, real
 * address, tunnel address, data totals, connected since), read through pfSense's
 * openvpn_get_active_servers() and posted to <app>/api/v1/vpn-status with the same token. Read-only:
 * nothing about the VPN is changed. A failure here never stops the block-list sync below.
 *
 * The router trusts nothing from the app: a line is loaded only if it is a single IPv4 address or
 * a range no wider than /24, is a public internet address, and does not touch any address of this
 * router, its networks or its gateways, or anything in /root/netgalaxy/protect. At most 2000 lines.
 *
 * Files: /root/netgalaxy/url (the list's address), /root/netgalaxy/token (mode 600),
 *        /root/netgalaxy/protect (optional, one CIDR per line), /var/db/netgalaxy/ (state).
 */
const TABLE = 'pfB_NetGalaxy_v4';
const DIR = '/var/db/netgalaxy';
const MAX_LINES = 2000;
const MIN_PREFIX = 24;
const FEED = '/var/db/pfblockerng/netgalaxy.txt';

function readf($p) { return is_readable($p) ? trim(file_get_contents($p)) : ''; }
function logmsg($m) { openlog('netgalaxy-sync', LOG_PID, LOG_USER); syslog(LOG_NOTICE, $m); closelog(); }

$url = readf('/root/netgalaxy/url');
$token = readf('/root/netgalaxy/token');
if ($url === '' || $token === '') { exit(0); }
@mkdir(DIR, 0750, true);

// ---- VPN status (who is connected right now)
require_once('config.inc');
try {
    require_once('openvpn.inc');
    $servers = [];
    foreach (openvpn_get_active_servers() as $srv) {
        $conns = [];
        foreach ((array)($srv['conns'] ?? []) as $c) {
            $conns[] = array_intersect_key((array)$c, array_flip(['common_name', 'user_name', 'remote_host', 'virtual_addr',
                'bytes_recv', 'bytes_sent', 'connect_time_unix', 'cipher']));
        }
        $servers[] = ['name' => $srv['name'] ?? '', 'port' => $srv['port'] ?? '', 'mode' => $srv['mode'] ?? '', 'conns' => $conns];
    }
    $vpnurl = preg_replace('#/blocklist(\?.*)?$#', '/vpn-status', $url);
    if ($vpnurl !== $url) {
        $vctx = stream_context_create(['http' => ['method' => 'POST', 'timeout' => 10, 'ignore_errors' => true,
            'header' => "Content-Type: application/json\r\nX-NetGalaxy-Token: $token\r\n",
            'content' => json_encode(['at' => time(), 'servers' => $servers])]]);
        @file_get_contents($vpnurl, false, $vctx);
    }
} catch (Throwable $e) {
    logmsg('vpn status report failed: ' . $e->getMessage());
}

$have = readf(DIR . '/applied.hash');
$rejected = readf(DIR . '/rejected');

$ctx = stream_context_create(['http' => ['timeout' => 15, 'ignore_errors' => true]]);
$q = http_build_query(['token' => $token, 'have' => $have, 'rejected' => $rejected === '' ? null : $rejected]);
$body = @file_get_contents($url . (strpos($url, '?') === false ? '?' : '&') . $q, false, $ctx);
$status = isset($http_response_header[0]) ? $http_response_header[0] : '';
if ($body === false || strpos($status, ' 200') === false) { exit(0); }   // app down: keep what is loaded

$raw = hash('sha256', $body);
if ($raw === readf(DIR . '/raw.hash') && $have !== '') { exit(0); }      // nothing changed since last time

// ---- what may never be blocked: every address, network and gateway of this router
require_once('interfaces.inc');
require_once('gwlb.inc');
$protect = [];
foreach (get_configured_interface_list() as $if) {
    $ip = get_interface_ip($if);
    $bits = get_interface_subnet($if);
    if (is_ipaddrv4($ip)) {
        $protect[] = $ip . '/32';
        if (is_numeric($bits)) { $protect[] = gen_subnet($ip, $bits) . '/' . $bits; }
    }
    $gw = get_interface_gateway($if);
    if (is_ipaddrv4($gw)) { $protect[] = $gw . '/32'; }
}
foreach (explode("\n", readf('/root/netgalaxy/protect')) as $l) { if (is_subnetv4(trim($l)) || is_ipaddrv4(trim($l))) { $protect[] = trim($l); } }

function range4($cidr) {
    if (strpos($cidr, '/') === false) { $cidr .= '/32'; }
    list($ip, $bits) = explode('/', $cidr);
    $start = ip2long($ip) & (-1 << (32 - (int)$bits));
    return [$start, $start + (1 << (32 - (int)$bits)) - 1];
}
$guards = array_map('range4', $protect);
$global = defined('FILTER_FLAG_GLOBAL_RANGE') ? FILTER_FLAG_GLOBAL_RANGE : (FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE);

$ok = []; $bad = 0;
foreach (explode("\n", $body) as $line) {
    $t = trim($line);
    if ($t === '' || $t[0] === '#') { continue; }
    $parts = explode('/', $t);
    $bits = count($parts) === 2 ? $parts[1] : '32';
    if (count($parts) > 2 || !ctype_digit($bits) || (int)$bits < MIN_PREFIX || (int)$bits > 32
        || !filter_var($parts[0], FILTER_VALIDATE_IP, FILTER_FLAG_IPV4 | $global)) { $bad++; continue; }
    list($s, $e) = range4($t);
    foreach ($guards as $g) { if ($s <= $g[1] && $g[0] <= $e) { $bad++; continue 2; } }
    $ok[] = $t;
    if (count($ok) >= MAX_LINES) { break; }
}
$clean = implode('', array_map(function ($l) { return $l . "\n"; }, $ok));
file_put_contents(DIR . '/blocklist.txt.new', $clean);
exec('/sbin/pfctl -t ' . TABLE . ' -T replace -f ' . escapeshellarg(DIR . '/blocklist.txt.new') . ' 2>&1', $out, $rc);
if ($rc !== 0) { logmsg('pfctl failed: ' . implode(' ', $out)); exit(1); }
rename(DIR . '/blocklist.txt.new', DIR . '/blocklist.txt');
// The file pfBlockerNG's NetGalaxy list reads (it only reads its own folders). It always holds 192.0.2.1, an
// address reserved for documentation that is never used on the internet: pfBlockerNG drops the list's firewall
// rules while the list is empty, and it filters out loopback placeholders such as 127.1.7.7.
file_put_contents(FEED, "192.0.2.1\n" . $clean);
file_put_contents(DIR . '/applied.hash', substr(hash('sha256', $clean), 0, 16));
file_put_contents(DIR . '/raw.hash', $raw);
file_put_contents(DIR . '/rejected', (string)$bad);
logmsg(sprintf('loaded %d blocked address(es) into %s%s', count($ok), TABLE, $bad ? ", refused $bad" : ''));
