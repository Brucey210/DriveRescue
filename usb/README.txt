DRIVE RESCUE
============
Rescue files from a failing drive, or clone a healthy drive onto a bigger one.
It runs from a USB stick, so nothing needs installing on your computer.

WHAT YOU NEED
  * A USB stick of 8 GB or more (it will be erased once, in step 1).
  * A healthy drive with free space to save the rescued copy on
    (not needed for cloning).
  * The Windows PC you are making the stick on, with internet.

PART 1 - MAKE THE BOOT STICK (once)
  1. Download the SystemRescue ISO from https://www.system-rescue.org/Download/
  2. Download Rufus from https://rufus.ie and open it with the stick plugged in.
  3. In Rufus: pick the stick, pick the ISO, set
        Partition scheme: MBR      Target system: BIOS or UEFI
        File system: FAT32
     Leave the volume label as Rufus shows it (it looks like RESCUE1200).
  4. Press START. When asked, choose "ISO mode" (not DD mode).
     DD mode makes a read-only stick and the next step would not work.

PART 2 - ADD DRIVE RESCUE TO THE STICK
  1. Open the stick in File Explorer.
  2. Open DriveRescue-USB.zip and drag the two folders inside it,
     "autorun" and "drive-rescue", onto the stick (the top level, not inside
     another folder). Choose "Merge" if asked.

PART 3 - USE IT
  1. Plug the stick in, and plug in the drives you will use. Restart the PC and
     choose to boot from the USB stick (often F12, F10, F2 or Esc at start-up;
     look for "boot menu" in your PC's manual).
  2. At the SystemRescue menu, press Enter on the first option.
  3. The desktop appears and Drive Rescue opens by itself in a browser after
     a short wait. Follow the steps on screen.

IF IT DOES NOT OPEN BY ITSELF
  Open a terminal on the desktop and type:
        sh /run/archiso/bootmnt/drive-rescue/start.sh
  To only check that everything needed is present:
        sh /run/archiso/bootmnt/drive-rescue/start.sh --check

GOOD TO KNOW
  * Rescuing never writes to the failing drive. Cloning erases the target drive,
    and asks you to type ERASE first.
  * Leave the failing drive alone while it is copying. You can pause at any time.
  * Your rescued copy is saved in a folder (DriveRescue) on the healthy drive as
    an .img file, next to the files you recover. Nothing already on that drive is
    erased.
  * Secure Boot: if the stick will not start, turn Secure Boot off in the PC's
    firmware settings.
  * This program has been tested on pretend drives. Try it on something you can
    afford to lose first.
