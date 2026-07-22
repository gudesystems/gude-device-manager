<?php
declare(strict_types=1);

const GDM_REPO_API = 'https://api.github.com/repos/gudesystems/gude-device-manager/releases';
const GDM_TITLE = 'GUDE Device Manager';
const GDM_RELEASE_CACHE_TTL = 300;
const GDM_API_ALERT_INTERVAL = 3600;
const GDM_API_ALERT_EMAIL = 'fabian.neumann@gude-systems.com';

function string_starts_with(string $value, string $prefix): bool
{
    return substr($value, 0, strlen($prefix)) === $prefix;
}

function string_ends_with(string $value, string $suffix): bool
{
    if ($suffix === '') {
        return true;
    }
    return substr($value, -strlen($suffix)) === $suffix;
}

function string_contains(string $value, string $needle): bool
{
    return $needle === '' || strpos($value, $needle) !== false;
}

function runtime_path(string $filename): string
{
    return __DIR__ . DIRECTORY_SEPARATOR . $filename;
}

function valid_release_list($value): bool
{
    return is_array($value)
        && count($value) > 0
        && is_array($value[0])
        && isset($value[0]['tag_name']);
}

function load_release_cache(bool $freshOnly): ?array
{
    $path = runtime_path('.gdm-releases-cache.json');
    clearstatcache(true, $path);
    if (!is_file($path)) {
        return null;
    }
    if ($freshOnly && time() - (int) filemtime($path) > GDM_RELEASE_CACHE_TTL) {
        return null;
    }

    $body = file_get_contents($path);
    if ($body === false) {
        return null;
    }
    $decoded = json_decode($body, true);
    return valid_release_list($decoded) ? $decoded : null;
}

function write_release_cache(array $releases): void
{
    $path = runtime_path('.gdm-releases-cache.json');
    $temporary = tempnam(__DIR__, '.gdm-cache-');
    if ($temporary === false) {
        throw new RuntimeException('Unable to allocate release cache temporary file');
    }

    try {
        $json = json_encode($releases, JSON_UNESCAPED_SLASHES);
        if ($json === false || file_put_contents($temporary, $json . "\n", LOCK_EX) === false) {
            throw new RuntimeException('Unable to write release cache');
        }
        chmod($temporary, 0644);
        if (!rename($temporary, $path)) {
            throw new RuntimeException('Unable to publish release cache');
        }
        $temporary = null;
    } finally {
        if (is_string($temporary) && is_file($temporary)) {
            unlink($temporary);
        }
    }
}

function github_error_detail(string $body, string $transportError, int $status, array $responseHeaders): string
{
    $parts = [];
    if ($transportError !== '') {
        $parts[] = $transportError;
    }
    if ($status > 0) {
        $parts[] = 'HTTP ' . $status;
    }

    $decoded = json_decode($body, true);
    if (is_array($decoded) && isset($decoded['message'])) {
        $parts[] = (string) $decoded['message'];
    }
    foreach (['x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after'] as $name) {
        if (isset($responseHeaders[$name])) {
            $parts[] = $name . '=' . $responseHeaders[$name];
        }
    }
    return implode('; ', $parts) ?: 'unknown error';
}

function fetch_github_releases_from_api(): array
{
    $headers = [
        'Accept: application/vnd.github+json',
        'User-Agent: gdm-release-feed/1.0',
        'X-GitHub-Api-Version: 2022-11-28',
    ];

    if (function_exists('curl_init')) {
        $responseHeaders = [];
        $curl = curl_init(GDM_REPO_API);
        curl_setopt_array($curl, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => 30,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_HEADERFUNCTION => static function ($curlHandle, string $line) use (&$responseHeaders): int {
                $length = strlen($line);
                $separator = strpos($line, ':');
                if ($separator !== false) {
                    $name = strtolower(trim(substr($line, 0, $separator)));
                    $responseHeaders[$name] = trim(substr($line, $separator + 1));
                }
                return $length;
            },
        ]);
        $body = curl_exec($curl);
        $status = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        $error = curl_error($curl);
        curl_close($curl);

        if ($body === false || $status < 200 || $status >= 300) {
            throw new RuntimeException(
                'GitHub API request failed: '
                . github_error_detail(is_string($body) ? $body : '', $error, $status, $responseHeaders)
            );
        }
    } else {
        $context = stream_context_create([
            'http' => [
                'method' => 'GET',
                'header' => implode("\r\n", $headers) . "\r\n",
                'timeout' => 30,
            ],
        ]);
        $body = file_get_contents(GDM_REPO_API, false, $context);
        if ($body === false) {
            throw new RuntimeException('GitHub API request failed');
        }
    }

    $decoded = json_decode($body, true);
    if (!valid_release_list($decoded)) {
        throw new RuntimeException('GitHub API returned invalid JSON');
    }
    return $decoded;
}

function notify_github_api_error(Throwable $error, bool $usedStaleCache): void
{
    $statePath = runtime_path('.gdm-github-api-alert');
    $lock = fopen($statePath . '.lock', 'c');
    if ($lock === false) {
        error_log('GDM feed could not create alert lock: ' . $error->getMessage());
        return;
    }

    try {
        if (!flock($lock, LOCK_EX)) {
            error_log('GDM feed could not lock API alert state: ' . $error->getMessage());
            return;
        }
        clearstatcache(true, $statePath);
        if (is_file($statePath) && time() - (int) filemtime($statePath) < GDM_API_ALERT_INTERVAL) {
            return;
        }

        $subject = '[GDM release feed] GitHub API error';
        $body = implode("\n", [
            'Time: ' . gmdate('c'),
            'Host: ' . (gethostname() ?: 'unknown'),
            'API: ' . GDM_REPO_API,
            'Error: ' . $error->getMessage(),
            'Stale cache served: ' . ($usedStaleCache ? 'yes' : 'no'),
            '',
            'Further alerts are suppressed for ' . GDM_API_ALERT_INTERVAL . ' seconds.',
        ]);
        $headers = implode("\r\n", [
            'From: GDM Release Feed <noreply@gude-systems.com>',
            'Content-Type: text/plain; charset=UTF-8',
        ]);
        $sendmailPath = trim((string) ini_get('sendmail_path'));
        $mailAvailable = function_exists('mail')
            && ($sendmailPath === '' || !string_contains($sendmailPath, '/dev/null'));
        $sent = $mailAvailable && @mail(GDM_API_ALERT_EMAIL, $subject, $body, $headers);
        file_put_contents($statePath, gmdate('c') . "\n" . $error->getMessage() . "\n", LOCK_EX);
        chmod($statePath, 0644);
        if (!$sent) {
            $reason = $mailAvailable ? 'mail() failed' : 'mail transport unavailable';
            error_log('GDM feed could not send GitHub API alert email (' . $reason . '): ' . $error->getMessage());
        }
    } finally {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
}

function fetch_github_releases(): array
{
    $cached = load_release_cache(true);
    if ($cached !== null) {
        $GLOBALS['gdm_release_source'] = 'fresh-cache';
        return $cached;
    }

    $lock = fopen(runtime_path('.gdm-releases-cache.lock'), 'c');
    if ($lock === false) {
        throw new RuntimeException('Unable to create release cache lock');
    }

    try {
        if (!flock($lock, LOCK_EX)) {
            throw new RuntimeException('Unable to lock release cache refresh');
        }
        $cached = load_release_cache(true);
        if ($cached !== null) {
            $GLOBALS['gdm_release_source'] = 'fresh-cache';
            return $cached;
        }

        try {
            $releases = fetch_github_releases_from_api();
            write_release_cache($releases);
            $GLOBALS['gdm_release_source'] = 'github-api';
            return $releases;
        } catch (Throwable $error) {
            $stale = load_release_cache(false);
            notify_github_api_error($error, $stale !== null);
            if ($stale !== null) {
                $GLOBALS['gdm_release_source'] = 'stale-cache';
                error_log('GDM feed serving stale release cache: ' . $error->getMessage());
                return $stale;
            }
            throw $error;
        }
    } finally {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
}

function version_from_release(array $release): string
{
    $source = (string) ($release['tag_name'] ?? $release['name'] ?? '');
    if (preg_match('/(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*)/', $source, $matches)) {
        return $matches[1];
    }
    return ltrim($source, 'v');
}

function format_release_date($value): string
{
    if (!$value) {
        return '';
    }
    $date = new DateTimeImmutable($value);
    return $date->format('d.m.Y');
}

function format_asset_size($size): string
{
    if (!$size) {
        return '';
    }
    $mib = $size / 1024 / 1024;
    if ($mib >= 1) {
        return number_format($mib, 1, '.', '') . ' MB';
    }
    return number_format($size / 1024, 1, '.', '') . ' KB';
}

function pick_asset(array $release): array
{
    $assets = $release['assets'] ?? [];
    if (!is_array($assets) || count($assets) === 0) {
        return [];
    }

    $candidates = array_values(array_filter($assets, static function (array $asset): bool {
        return string_ends_with(strtolower((string) ($asset['name'] ?? '')), '.exe');
    }));
    if (count($candidates) === 0) {
        $candidates = $assets;
    }

    usort($candidates, static function (array $left, array $right): int {
        return ((int) ($right['size'] ?? 0)) <=> ((int) ($left['size'] ?? 0));
    });
    return $candidates[0];
}

function clean_markdown(string $value): string
{
    $value = preg_replace('/`([^`]+)`/', '$1', $value);
    $value = preg_replace('/\*\*([^*]+)\*\*/', '$1', $value);
    $value = preg_replace('/\[([^\]]+)\]\([^)]+\)/', '$1', $value);
    return trim((string) $value);
}

function extract_section_bullets(string $markdown, string $heading): array
{
    $lines = preg_split('/\R/', $markdown) ?: [];
    $bullets = [];
    $inSection = false;

    foreach ($lines as $line) {
        $stripped = trim($line);
        if (preg_match('/^###\s+' . preg_quote($heading, '/') . '\s*$/i', $stripped)) {
            $inSection = true;
            continue;
        }
        if ($inSection && string_starts_with($stripped, '### ')) {
            break;
        }
        if (!$inSection) {
            continue;
        }
        if (preg_match('/^[-*]\s+(.+?)\s*$/', $stripped, $matches)) {
            $bullets[] = clean_markdown($matches[1]);
        }
    }

    return $bullets;
}

function split_summary(array $summary): array
{
    $features = [];
    $fixes = [];
    foreach ($summary as $item) {
        $lower = strtolower($item);
        if (string_starts_with($lower, 'fix:') || string_starts_with($lower, 'fixed ') || string_contains($lower, ' bug')) {
            $fixes[] = $item;
        } else {
            $features[] = $item;
        }
    }
    return [$features, $fixes];
}

function release_to_entry(array $release): array
{
    $asset = pick_asset($release);
    $summary = extract_section_bullets((string) ($release['body'] ?? ''), 'Summary');
    if (count($summary) === 0) {
        $summary = extract_section_bullets((string) ($release['body'] ?? ''), 'Highlights');
    }
    [$features, $fixes] = split_summary($summary);

    return [
        'version' => version_from_release($release),
        'date' => format_release_date($release['published_at'] ?? $release['created_at'] ?? null),
        'size' => format_asset_size(isset($asset['size']) ? (int) $asset['size'] : null),
        'features' => $features,
        'fixes' => $fixes,
        '_filename' => (string) ($asset['name'] ?? ''),
        '_download_url' => (string) ($asset['browser_download_url'] ?? ''),
    ];
}

function public_entry(array $entry): array
{
    return [
        'version' => $entry['version'],
        'date' => $entry['date'],
        'size' => $entry['size'],
        'features' => $entry['features'],
        'fixes' => $entry['fixes'],
    ];
}

function build_entries(bool $includePrereleases): array
{
    $entries = [];
    foreach (fetch_github_releases() as $release) {
        if (!is_array($release) || ($release['draft'] ?? false)) {
            continue;
        }
        if (!$includePrereleases && ($release['prerelease'] ?? false)) {
            continue;
        }
        $entries[] = release_to_entry($release);
    }
    return $entries;
}

function render_html(array $entries): string
{
    $out = [];
    $out[] = '<!DOCTYPE html>';
    $out[] = '<html>';
    $out[] = '  <head>';
    $out[] = '    <title>' . htmlspecialchars(GDM_TITLE, ENT_QUOTES, 'UTF-8') . ' - Revision History</title>';
    $out[] = '    <style>';
    $out[] = '      body {';
    $out[] = '        font-family: Arial, Helvetica, sans-serif;';
    $out[] = '        background-color: white;';
    $out[] = '      }';
    $out[] = '    </style>';
    $out[] = '  </head>';
    $out[] = '  <body>';
    $out[] = '    <h1>' . htmlspecialchars(GDM_TITLE, ENT_QUOTES, 'UTF-8') . '</h1>';
    $out[] = '    <h2>Software Release Notes / Revision History</h2>';
    $out[] = '';

    foreach ($entries as $entry) {
        $filename = 'gdm_v' . $entry['version'] . '.zip';
        $versionHtml = '<ul><li><a href="' . htmlspecialchars($filename, ENT_QUOTES, 'UTF-8') . '">v'
            . htmlspecialchars($entry['version'], ENT_QUOTES, 'UTF-8') . ' - '
            . htmlspecialchars($entry['date'], ENT_QUOTES, 'UTF-8') . '</a></li>';
        if (count($entry['features']) > 0) {
            $versionHtml .= '<ul><li><b>Features</b></li><ul>';
            foreach ($entry['features'] as $item) {
                $versionHtml .= '<li>' . htmlspecialchars($item, ENT_QUOTES, 'UTF-8') . '</li>';
            }
            $versionHtml .= '</ul></ul>';
        }
        if (count($entry['fixes']) > 0) {
            $versionHtml .= '<ul><li><b>Bugfixes</b></li><ul>';
            foreach ($entry['fixes'] as $item) {
                $versionHtml .= '<li>' . htmlspecialchars($item, ENT_QUOTES, 'UTF-8') . '</li>';
            }
            $versionHtml .= '</ul></ul>';
        }
        $out[] = $versionHtml . '</ul>';
    }

    $out[] = '<hr size="1" noshade />';
    $out[] = '<img src="https://www.gude.info/fileadmin/templates/img/logo_gude.png">';
    $out[] = '<br />';
    $out[] = '<a href="https://www.gude.info">GUDE Systems GmbH</a>';
    $out[] = '</body>';
    $out[] = '</html>';
    $out[] = '';
    return implode("\n", $out);
}

function requested_format(): string
{
    $format = strtolower((string) ($_GET['format'] ?? ''));
    if ($format === 'json' || $format === 'html') {
        return $format;
    }

    $path = strtolower((string) parse_url($_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH));
    if (string_ends_with($path, '.json')) {
        return 'json';
    }
    if (preg_match('/\/gdm_v[^\/]+\.zip$/', $path)) {
        return 'download';
    }
    return 'html';
}

function requested_download_version(): string
{
    $path = (string) parse_url($_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH);
    if (preg_match('/\/gdm_v([^\/]+)\.zip$/i', $path, $matches)) {
        return rawurldecode($matches[1]);
    }
    return '';
}

function download_release_asset(string $url, string $destination): void
{
    $host = strtolower((string) parse_url($url, PHP_URL_HOST));
    if ($host !== 'github.com') {
        throw new RuntimeException('Refusing unexpected release asset host');
    }

    $output = fopen($destination, 'wb');
    if ($output === false) {
        throw new RuntimeException('Unable to create temporary executable');
    }

    $curl = curl_init($url);
    if ($curl === false) {
        fclose($output);
        throw new RuntimeException('Unable to initialize release download');
    }

    try {
        curl_setopt_array($curl, [
            CURLOPT_FILE => $output,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_CONNECTTIMEOUT => 15,
            CURLOPT_TIMEOUT => 300,
            CURLOPT_USERAGENT => 'gdm-release-feed/1.0',
            CURLOPT_FAILONERROR => false,
        ]);
        $success = curl_exec($curl);
        $status = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        $error = curl_error($curl);
        if ($success === false || $status < 200 || $status >= 300) {
            throw new RuntimeException('Release download failed: ' . ($error ?: 'HTTP ' . $status));
        }
    } finally {
        curl_close($curl);
        fclose($output);
    }

    clearstatcache(true, $destination);
    if (!is_file($destination) || filesize($destination) < 2) {
        throw new RuntimeException('Downloaded release asset is empty');
    }

    $input = fopen($destination, 'rb');
    if ($input === false) {
        throw new RuntimeException('Unable to validate downloaded executable');
    }
    try {
        $signature = fread($input, 2);
    } finally {
        fclose($input);
    }
    if ($signature !== 'MZ') {
        throw new RuntimeException('Downloaded release asset is not a Windows executable');
    }
}

function cached_zip_for_entry(array $entry): string
{
    if (!class_exists('ZipArchive')) {
        throw new RuntimeException('PHP ZipArchive extension is unavailable');
    }
    if (!preg_match('/^[0-9A-Za-z.-]+$/', $entry['version'])) {
        throw new RuntimeException('Release has an unsafe version name');
    }

    $assetFilename = basename(str_replace('\\', '/', $entry['_filename']));
    if ($assetFilename === '' || !string_ends_with(strtolower($assetFilename), '.exe')) {
        throw new RuntimeException('Release does not contain a Windows executable');
    }

    $archiveFilename = 'gdm_v' . $entry['version'] . '.zip';
    $archivePath = __DIR__ . DIRECTORY_SEPARATOR . $archiveFilename;
    clearstatcache(true, $archivePath);
    if (is_file($archivePath) && filesize($archivePath) > 0) {
        return $archivePath;
    }

    $lock = fopen($archivePath . '.lock', 'c');
    if ($lock === false) {
        throw new RuntimeException('Unable to create archive lock');
    }

    $temporaryExecutable = null;
    $temporaryArchive = null;
    try {
        if (!flock($lock, LOCK_EX)) {
            throw new RuntimeException('Unable to lock archive generation');
        }

        clearstatcache(true, $archivePath);
        if (is_file($archivePath) && filesize($archivePath) > 0) {
            return $archivePath;
        }

        $temporaryExecutable = tempnam(__DIR__, '.gdm-exe-');
        $temporaryArchive = tempnam(__DIR__, '.gdm-zip-');
        if ($temporaryExecutable === false || $temporaryArchive === false) {
            throw new RuntimeException('Unable to allocate archive temporary files');
        }

        download_release_asset($entry['_download_url'], $temporaryExecutable);

        $zip = new ZipArchive();
        $result = $zip->open($temporaryArchive, ZipArchive::CREATE | ZipArchive::OVERWRITE);
        if ($result !== true) {
            throw new RuntimeException('Unable to create release archive: error ' . $result);
        }
        if (!$zip->addFile($temporaryExecutable, $assetFilename)) {
            $zip->close();
            throw new RuntimeException('Unable to add executable to release archive');
        }
        $zip->setCompressionName($assetFilename, ZipArchive::CM_STORE);
        if (!$zip->close()) {
            throw new RuntimeException('Unable to finalize release archive');
        }

        clearstatcache(true, $temporaryArchive);
        if (!is_file($temporaryArchive) || filesize($temporaryArchive) === 0) {
            throw new RuntimeException('Generated release archive is empty');
        }
        chmod($temporaryArchive, 0644);
        if (!rename($temporaryArchive, $archivePath)) {
            throw new RuntimeException('Unable to publish generated release archive');
        }
        $temporaryArchive = null;
        return $archivePath;
    } finally {
        if (is_string($temporaryExecutable) && is_file($temporaryExecutable)) {
            unlink($temporaryExecutable);
        }
        if (is_string($temporaryArchive) && is_file($temporaryArchive)) {
            unlink($temporaryArchive);
        }
        flock($lock, LOCK_UN);
        fclose($lock);
    }
}

function serve_zip(string $path): void
{
    clearstatcache(true, $path);
    header('Content-Type: application/zip');
    header('Content-Disposition: attachment; filename="' . basename($path) . '"');
    header('Content-Length: ' . filesize($path));
    header('Cache-Control: public, max-age=31536000, immutable');
    header('X-Content-Type-Options: nosniff');
    if (strtoupper((string) ($_SERVER['REQUEST_METHOD'] ?? 'GET')) !== 'HEAD') {
        readfile($path);
    }
}

try {
    $includePrereleases = isset($_GET['prereleases']) && $_GET['prereleases'] !== '0';
    $entries = build_entries($includePrereleases);
    $format = requested_format();
    header('X-GDM-Release-Source: ' . ($GLOBALS['gdm_release_source'] ?? 'unknown'));

    if ($format === 'download') {
        $version = requested_download_version();
        foreach ($entries as $entry) {
            if ($entry['version'] === $version && $entry['_download_url'] !== '') {
                serve_zip(cached_zip_for_entry($entry));
                exit;
            }
        }
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo "Unknown GUDE Device Manager version\n";
    } elseif ($format === 'json') {
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: public, max-age=300');
        echo json_encode(array_map('public_entry', $entries), JSON_UNESCAPED_SLASHES) . "\n";
    } else {
        header('Content-Type: text/html; charset=utf-8');
        header('Cache-Control: public, max-age=300');
        echo render_html($entries);
    }
} catch (Throwable $error) {
    http_response_code(502);
    header('Content-Type: text/plain; charset=utf-8');
    echo 'Unable to build GUDE Device Manager release feed: ' . $error->getMessage() . "\n";
}
