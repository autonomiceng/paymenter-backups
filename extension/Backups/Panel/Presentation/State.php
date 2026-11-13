<?php
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Presentation;
use Filament\Support\Contracts\HasLabel;
/** The closed list of status words on Backups pages, owned here and never synced. The shared block (results and the
 * availability states) keeps Domains' words and tones; the rest are the words Backups already shows, merged as the
 * design rules say (Waiting and Starting into Queued, Done into Completed, Interrupted and Didn't start into Failed,
 * Test again into Out of date). A page passes a case and never types the word, colour or mark. Only staff read these
 * pages, so every case has one tone. HasLabel only: a State is never a Filament colour. */
enum State: string implements HasLabel
{
    #[Entry([State::Verified, State::Completed], 'A check that passed. Verified is a backup and Completed is a task.', 'Shared block. DESIGN.md Panels, Nov 12, 2026.')]
    case Ok = 'ok';
    #[Entry([State::Ok, State::InS3], 'A backup on this server whose copy passed its check, so it can be downloaded.', 'Backups\' existing word. DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Verified = 'verified';
    #[Entry([State::Verified], 'A verified backup kept only in S3. Get from S3 brings it back to download.', 'Backups\' existing word.')]
    case InS3 = 'in_s3';
    #[Entry([State::Ok, State::Verified], 'A task that finished. OK is a check that passed.', 'Done merged into it. DESIGN.md Panels, Nov 12, 2026.')]
    case Completed = 'completed';

    #[Entry([State::Checking, State::CouldntRead], 'A value never read, or waiting its turn. Checking is a read happening now.', 'Shared block. Ilya, Nov 12, 2026, decision 4.')]
    case NotReadYet = 'not_read_yet';
    #[Entry([State::NotSupported], 'Something we don\'t offer by choice.', 'Shared block. Ilya, Nov 12, 2026, decision 2.')]
    case NotForSale = 'not_for_sale';
    #[Entry([State::NotForSale], 'Something this server can\'t do at all.', 'Shared block. Ilya, Nov 12, 2026, decisions 2 and 3.')]
    case NotSupported = 'not_supported';
    #[Entry([State::NotReadYet, State::CouldntRead], 'The runner sent a status we have not mapped, and the code shows under it.', 'Backups\' existing word, shared by every repo. DESIGN.md Panels, Nov 12, 2026.')]
    case NotRecognized = 'not_recognized';
    #[Entry([State::Skipped, State::Cancelled], 'A backup deleted on purpose, by cleanup or by staff.', 'Backups\' existing word. DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Deleted = 'deleted';
    #[Entry([State::Cancelled, State::Covered], 'A task that did not run because it wasn\'t needed.', 'Backups\' existing word. DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Skipped = 'skipped';
    #[Entry([State::Skipped, State::Failed], 'A task stopped on purpose before it ran.', 'DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Cancelled = 'cancelled';
    #[Entry([State::Skipped, State::Completed], 'A task another backup already did, such as a database backup a full backup covered.', 'Backups\' existing word.')]
    case Covered = 'covered';

    #[Entry([State::Queued, State::Running], 'A read or check waiting on something outside this server now.', 'Shared block. Ilya, Nov 12, 2026, M0 rounds 4 and 5.')]
    case Checking = 'checking';
    #[Entry([State::Running, State::Checking], 'Waiting to start. The reason says what it waits for.', 'Waiting and Starting merged into it. DESIGN.md Panels, Nov 12, 2026.')]
    case Queued = 'queued';
    #[Entry([State::Queued, State::Deleting], 'A backup or task running on this server now.', 'DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Running = 'running';
    #[Entry([State::Running, State::Deleted], 'A backup being deleted now.', 'Backups\' existing word. DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Deleting = 'deleting';

    #[Entry([State::NeedsReview, State::NotReadYet], 'A setting staff must choose is empty, such as S3 keys for uploads.', 'Shared block. Replaces Missing on settings. Ilya, Nov 12, 2026, decision 4.')]
    case NotSet = 'not_set';
    #[Entry([State::NotSet, State::Failed], 'A check found something staff must fix before backups can run, and nothing broke.', 'Not ready and Not saved merged into it. DESIGN.md Panels, Nov 12, 2026.')]
    case NeedsReview = 'needs_review';
    #[Entry([State::NeedsReview, State::Failed], 'Paymenter\'s cron job has not run lately, so nothing scheduled starts.', 'DESIGN.md Panels stock words, Nov 12, 2026.')]
    case NotRunning = 'not_running';
    #[Entry([State::NotReadYet, State::NeedsReview], 'A result that was true once but settings changed since, like the storage test.', 'Stale and Test again merged into it. DESIGN.md Panels, Nov 12, 2026.')]
    case OutOfDate = 'out_of_date';
    #[Entry([State::Verified, State::Damaged], 'A backup whose copy has not been checked since it changed.', 'Backups\' existing word.')]
    case NotVerified = 'not_verified';
    #[Entry([State::Failed, State::NeedsReview], 'An interrupted full backup left something to fix before new backups start.', 'Backups\' existing word.')]
    case NeedsRecovery = 'needs_recovery';
    #[Entry([State::Deleting, State::Failed], 'A deletion stopped part way. It resumes on its own.', 'Backups\' existing word.')]
    case DeletionIncomplete = 'deletion_incomplete';
    #[Entry([State::S3CopyDamaged, State::OldS3Folder], 'The S3 copy is gone from the bucket.', 'Backups\' existing word.')]
    case S3CopyMissing = 's3_copy_missing';
    #[Entry([State::S3CopyMissing], 'The S3 copy is in a folder from earlier settings.', 'Backups\' existing word.')]
    case OldS3Folder = 'old_s3_folder';
    #[Entry([State::S3CopyMissing, State::NotVerified], 'An S3 copy not yet checked against this backup.', 'Backups\' existing word.')]
    case S3CopyUnchecked = 's3_copy_unchecked';

    #[Entry([State::Failed, State::NotReadYet], 'The last read of a value failed.', 'Shared block. Ilya, Nov 12, 2026, decision 4.')]
    case CouldntRead = 'couldnt_read';
    #[Entry([State::CouldntRead, State::UploadFailed], 'A backup, task or check that broke. The reason says what.', 'Interrupted and Didn\'t start merged into it. DESIGN.md Panels, Nov 12, 2026.')]
    case Failed = 'failed';
    #[Entry([State::Failed], 'A backup kept here whose S3 upload failed. Red, because nothing off this server holds it.', 'Ilya, Nov 12, 2026, plan r2 decision 4.')]
    case UploadFailed = 'upload_failed';
    #[Entry([State::Failed, State::S3CopyDamaged], 'A backup on this server that failed its check.', 'DESIGN.md Panels seed list, Nov 12, 2026.')]
    case Damaged = 'damaged';
    #[Entry([State::Damaged], 'The S3 copy failed its check.', 'Backups\' existing word.')]
    case S3CopyDamaged = 's3_copy_damaged';
    #[Entry([State::Damaged, State::Failed], 'A backup with no usable copy anywhere.', 'DESIGN.md Panels seed list, Nov 12, 2026.')]
    case NotUsable = 'not_usable';
    #[Entry([State::Failed, State::Cancelled], 'A task that waited past its limit and won\'t run. Red for every reader.', 'Ilya, Nov 12, 2026, plan r2 decision 4.')]
    case Expired = 'expired';

    /** The block every repo shares with Domains: results and the six availability states. */
    public const SHARED = [self::Ok, self::Failed, self::NotReadYet, self::Checking, self::CouldntRead, self::NotSet, self::NotForSale, self::NotSupported, self::NotRecognized];

    public function label(): string
    {
        return match ($this) {
            self::Ok => 'OK', self::Verified => 'Verified', self::InS3 => 'In S3', self::Completed => 'Completed',
            self::NotReadYet => 'Not read yet', self::NotForSale => 'Not for sale', self::NotSupported => 'Not supported', self::NotRecognized => 'Not recognized',
            self::Deleted => 'Deleted', self::Skipped => 'Skipped', self::Cancelled => 'Cancelled', self::Covered => 'Covered',
            self::Checking => 'Checking', self::Queued => 'Queued', self::Running => 'Running', self::Deleting => 'Deleting',
            self::NotSet => 'Not set', self::NeedsReview => 'Needs review', self::NotRunning => 'Not running', self::OutOfDate => 'Out of date',
            self::NotVerified => 'Not verified', self::NeedsRecovery => 'Needs recovery', self::DeletionIncomplete => 'Deletion incomplete',
            self::S3CopyMissing => 'S3 copy missing', self::OldS3Folder => 'Old S3 folder', self::S3CopyUnchecked => 'S3 copy unchecked',
            self::CouldntRead => 'Couldn\'t read', self::Failed => 'Failed', self::UploadFailed => 'Upload failed', self::Damaged => 'Damaged',
            self::S3CopyDamaged => 'S3 copy damaged', self::NotUsable => 'Not usable', self::Expired => 'Expired',
        };
    }
    public function getLabel(): string { return $this->label(); }
    /** Only staff read Backups pages, so no state differs by reader. */
    public function tone(Reader $reader): Tone
    {
        return match ($this) {
            self::Ok, self::Verified, self::InS3, self::Completed => Tone::Ok,
            self::NotReadYet, self::NotForSale, self::NotSupported, self::NotRecognized, self::Deleted, self::Skipped, self::Cancelled, self::Covered => Tone::Off,
            self::Checking, self::Queued, self::Running, self::Deleting => Tone::Progress,
            self::NotSet, self::NeedsReview, self::NotRunning, self::OutOfDate, self::NotVerified, self::NeedsRecovery, self::DeletionIncomplete,
            self::S3CopyMissing, self::OldS3Folder, self::S3CopyUnchecked => Tone::Attention,
            self::CouldntRead, self::Failed, self::UploadFailed, self::Damaged, self::S3CopyDamaged, self::NotUsable, self::Expired => Tone::Failed,
        };
    }
}
