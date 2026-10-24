<?php
namespace Symfony\Component\Process {
    final class Process
    {
        private string $output = '';
        private string $errorOutput = '';
        private bool $successful = true;

        public function __construct(private array $command, ?string $cwd = null, ?array $env = null, ?string $input = null) {}
        public function setTimeout(?float $timeout): void {}
        public function mustRun(): self { $this->run(); if (!$this->successful) { throw new \RuntimeException('process-failed'); } return $this; }
        public function run(): void
        {
            if (in_array('-c', $this->command, true)) {
                $this->output = json_encode([
                    'python_valid' => true,
                    'php_valid' => true,
                    'php_canonical' => PHP_BINARY,
                    'current_canonical' => PHP_BINARY,
                ], JSON_THROW_ON_ERROR);
            } elseif (in_array('-r', $this->command, true)) {
                $this->output = 'cli 80300';
            } else {
                $this->successful = false;
                $this->errorOutput = '{"error":"busy"}';
            }
        }
        public function getOutput(): string { return $this->output; }
        public function getErrorOutput(): string { return $this->errorOutput; }
        public function isSuccessful(): bool { return $this->successful; }
    }
}

namespace Illuminate\Database {
    final class ConfigurationUrlParser
    {
        public function parseConfiguration(array $connection): array { return $connection; }
    }
}

namespace Illuminate\Support\Facades {
    final class Auth { public static function id(): ?int { return null; } }
}

namespace App\Models {
    final class Extension
    {
        public static function __callStatic(string $name, array $arguments): self { return new self(); }
        public function __call(string $name, array $arguments): self { return $this; }
        public function settings(): SettingsQuery { return new SettingsQuery(); }
    }
    final class SettingsQuery
    {
        private string $key = '';
        public function where(string $column, string $key): self { $this->key = $key; return $this; }
        public function value(string $column): ?string
        {
            return match ($this->key) {
                'private_root' => null,
                'extra_docroots' => '[]',
                'php' => PHP_BINARY,
                'python' => '/usr/bin/python3',
                default => null,
            };
        }
    }
}

namespace Paymenter\Extensions\Others\Backups {
    final class PrivateStore
    {
        public static function root(string $app, ?string $configured = null, bool $create = false, array $extraDocroots = []): string
        {
            return sys_get_temp_dir() . '/backups-cause-free-test';
        }
    }
}

namespace {
    final class FixtureConfig
    {
        private array $connection = [
            'driver' => 'mariadb', 'host' => '127.0.0.1', 'port' => 3306, 'unix_socket' => '',
            'database' => 'fixture', 'username' => 'fixture', 'password' => 'fixture', 'prefix' => '',
            'options' => [], 'charset' => 'utf8mb4', 'collation' => 'utf8mb4_unicode_ci',
            'strict' => true, 'engine' => null, 'prefix_indexes' => true,
        ];
        public function get(string $key, mixed $default = null): mixed
        {
            return match ($key) {
                'database.default' => 'mariadb',
                'database.connections.mariadb' => $this->connection,
                default => $default,
            };
        }
    }

    function app(string $service): FixtureConfig { return $GLOBALS['fixture_config']; }
    function base_path(): string { return dirname(__DIR__, 2); }

    $GLOBALS['fixture_config'] = new FixtureConfig();
    require __DIR__ . '/../../extension/Backups/Settings.php';
    require __DIR__ . '/../../extension/Backups/Messages.php';
    require __DIR__ . '/../../extension/Backups/Runtime.php';

    set_error_handler(static function (int $severity, string $message): never {
        throw new \ErrorException($message, 0, $severity);
    }, E_WARNING);

    try {
        \Paymenter\Extensions\Others\Backups\Runtime::saveConfiguration(['enabled' => true]);
        echo 'unexpected-success';
    } catch (\Throwable $error) {
        echo get_class($error) . ':' . $error->getMessage();
    } finally {
        restore_error_handler();
    }
}
