<?php
// No Laravel boot or network transport: the real command and private ledger run
// in separate PHP processes, with an inert mail facade and console stand-in.
namespace Illuminate\Console {
    class Command {
        public const SUCCESS = 0;
        public const FAILURE = 1;
        public function line($line): void {}
        public function error($line): void {}
    }
}
namespace Illuminate\Support\Facades {
    final class Mail {
        public static function raw($body, $callback): object {
            if ($GLOBALS['argv'][4] === 'fail') { throw new \RuntimeException('fake-canary'); }
            $mail = new class {
                public string $to;
                public string $subject;
                public function to($email): self { $this->to = $email; return $this; }
                public function subject($subject): self { $this->subject = $subject; return $this; }
            };
            $callback($mail);
            $outbox = is_file($GLOBALS['argv'][3]) ? json_decode(file_get_contents($GLOBALS['argv'][3]), true) : [];
            $outbox[] = ['to' => $mail->to, 'subject' => $mail->subject, 'body' => $body];
            file_put_contents($GLOBALS['argv'][3], json_encode($outbox, JSON_THROW_ON_ERROR));
            return $mail;
        }
    }
}
namespace Paymenter\Extensions\Others\Backups {
    final class Runtime {
        public static function privateRoot(): string { return $GLOBALS['argv'][1]; }
        public static function command($arguments, $input, $timeout): array {
            if ($arguments !== ['observe']) { throw new \RuntimeException('unexpected command'); }
            return json_decode(file_get_contents($GLOBALS['argv'][2]), true, 32, JSON_THROW_ON_ERROR);
        }
    }
}
namespace {
    function config($key) {
        return match ($key) {
            'settings.system_email_address' => 'staff@example.invalid',
            'settings.mail_disable' => $GLOBALS['argv'][5] === 'disabled',
            default => throw new \RuntimeException('unexpected config'),
        };
    }
    $extension = dirname(__DIR__, 2) . '/extension/Backups/';
    require $extension . 'PrivateStore.php';
    require $extension . 'Messages.php';
    require $extension . 'ObserveCommand.php';
    $status = (new \Paymenter\Extensions\Others\Backups\ObserveCommand)->handle();
    $outbox = is_file($argv[3]) ? json_decode(file_get_contents($argv[3]), true) : [];
    echo json_encode(['status' => $status, 'sent' => count($outbox)], JSON_THROW_ON_ERROR);
}
